"""Independent hard-veto risk planner. It does not import or modify strategy internals."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping, Sequence

from app.strategy.rfpc import TradeCandidate


@dataclass(frozen=True, slots=True)
class RiskConfig:
    risk_per_trade_fraction: float = 0.005
    max_open_risk_fraction: float = 0.02
    max_position_notional_multiple: float = 3.0
    max_total_notional_multiple: float = 5.0
    max_concurrent_positions: int = 4
    max_drawdown_fraction: float = 0.10
    max_daily_loss_fraction: float = 0.02
    max_cost_to_risk: float = 0.25
    fee_rate_per_side: float = 0.0008
    slippage_rate_per_side: float = 0.0005


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str
    quantity: float = 0.0
    risk_budget: float = 0.0
    modeled_stop_loss: float = 0.0
    modeled_cost: float = 0.0
    notional: float = 0.0
    evidence: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def paper_ledger_risk_metrics(
    positions: Sequence[Mapping[str, Any]], *, starting_balance: float, now_epoch: int,
) -> dict[str, float]:
    """Return realized-only daily-loss and peak-to-current drawdown fractions.

    The paper ledger stores realized P&L, not live mark-to-market valuations. Open
    positions are therefore intentionally excluded; callers must not describe these
    figures as unrealized-risk or liquidation-aware account equity.
    """
    if not math.isfinite(float(starting_balance)) or starting_balance <= 0:
        raise ValueError("starting_balance must be finite and positive")
    now_epoch = int(now_epoch)
    day_start = (now_epoch // 86400) * 86400
    closed: list[tuple[int, float]] = []
    for position in positions:
        if position.get("status") != "closed" or position.get("exit_time") is None:
            continue
        try:
            exit_time = int(position["exit_time"])
            pnl = float(position.get("realised_pnl") or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(pnl) and exit_time <= now_epoch:
            closed.append((exit_time, pnl))
    # Aggregate same-timestamp closes before updating the equity curve. Otherwise
    # simultaneous exits could produce an arbitrary drawdown based on database row order.
    pnl_by_exit_time: dict[int, float] = {}
    for timestamp, pnl in closed:
        pnl_by_exit_time[timestamp] = pnl_by_exit_time.get(timestamp, 0.0) + pnl
    realised_path = sorted(pnl_by_exit_time.items())

    equity_before_day = float(starting_balance) + sum(
        pnl for timestamp, pnl in realised_path if timestamp < day_start
    )
    today_pnl = sum(
        pnl for timestamp, pnl in realised_path if day_start <= timestamp <= now_epoch
    )
    daily_loss_fraction = max(0.0, -today_pnl) / max(equity_before_day, 1e-12)

    equity = float(starting_balance)
    peak = equity
    for _, pnl in realised_path:
        equity += pnl
        peak = max(peak, equity)
    current_drawdown_fraction = max(0.0, peak - equity) / max(peak, 1e-12)
    return {
        "daily_loss_fraction": max(0.0, daily_loss_fraction),
        "current_drawdown_fraction": max(0.0, current_drawdown_fraction),
        "today_realised_pnl": today_pnl,
        "equity_before_day": equity_before_day,
        "realised_equity": equity,
        "drawdown_peak_equity": peak,
    }


def _round_down_step(value: float, step: float) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    value_d, step_d = Decimal(str(value)), Decimal(str(step))
    return float((value_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d)


class RiskEngine:
    """Risk approval is independent of candidate quality and optional AI output."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def evaluate(
        self,
        candidate: TradeCandidate,
        *,
        equity: float,
        contract: Mapping[str, Any] | None,
        open_positions: Sequence[Mapping[str, Any]] = (),
        daily_loss_fraction: float = 0.0,
        current_drawdown_fraction: float = 0.0,
        data_is_fresh: bool = True,
        account_state_known: bool = True,
        reconciliation_clean: bool = True,
        halt_active: bool = False,
        estimated_funding_cost_per_contract: float = 0.0,
    ) -> RiskDecision:
        def reject(reason: str, **evidence: Any) -> RiskDecision:
            return RiskDecision(False, reason, evidence=evidence)
        if halt_active:
            return reject("KILL_SWITCH_ACTIVE")
        if not data_is_fresh:
            return reject("STALE_OR_INVALID_DATA")
        if not account_state_known or not reconciliation_clean:
            return reject("ACCOUNT_OR_POSITION_STATE_UNCERTAIN")
        if equity <= 0 or not math.isfinite(equity):
            return reject("INVALID_EQUITY")
        if daily_loss_fraction >= self.config.max_daily_loss_fraction:
            return reject("DAILY_LOSS_LIMIT")
        if current_drawdown_fraction >= self.config.max_drawdown_fraction:
            return reject("DRAWDOWN_LIMIT")
        if len(open_positions) >= self.config.max_concurrent_positions:
            return reject("MAX_CONCURRENT_POSITIONS")
        if not contract:
            return reject("CONTRACT_METADATA_MISSING")
        if contract.get("state") not in (0, "0"):
            return reject("CONTRACT_NOT_ENABLED", state=contract.get("state"))
        if contract.get("apiAllowed") not in (True, 1, "1", "true", "True"):
            return reject("CONTRACT_API_NOT_ALLOWED")
        try:
            contract_size = float(contract.get("contractSize", 0))
            vol_step = float(contract.get("volUnit", 0))
            min_vol = float(contract.get("minVol", 0))
            max_vol = float(contract.get("maxVol", float("inf")))
            tick_size = float(contract.get("priceUnit", 0))
        except (TypeError, ValueError):
            return reject("INVALID_CONTRACT_PRECISION")
        if min(contract_size, vol_step, min_vol, tick_size) <= 0 or max_vol < min_vol:
            return reject("INVALID_CONTRACT_PRECISION")
        entry = float(candidate.entry_reference)
        stop = float(candidate.stop_price)
        if entry <= 0 or stop <= 0:
            return reject("INVALID_ENTRY_OR_STOP")
        if (candidate.side == "LONG" and stop >= entry) or (candidate.side == "SHORT" and stop <= entry):
            return reject("STOP_NOT_ON_LOSS_SIDE")
        stop_distance = abs(entry - stop)
        risk_budget = equity * self.config.risk_per_trade_fraction
        fee_rate = self.config.fee_rate_per_side
        slip = self.config.slippage_rate_per_side
        unit_stop = stop_distance * contract_size
        unit_notional = entry * contract_size
        unit_fees = unit_notional * fee_rate + max(entry, float(candidate.target_price), stop) * contract_size * fee_rate
        unit_slippage = (entry + max(float(candidate.target_price), stop)) * contract_size * slip
        unit_funding = max(0.0, estimated_funding_cost_per_contract)
        unit_risk = unit_stop + unit_fees + unit_slippage + unit_funding
        if unit_risk <= 0:
            return reject("INVALID_MODELED_RISK")
        max_qty_by_position_notional = equity * self.config.max_position_notional_multiple / unit_notional
        max_qty_by_total_notional = max(0.0, equity * self.config.max_total_notional_multiple - sum(
            float(p.get("notional", 0.0)) for p in open_positions
        )) / unit_notional
        raw_qty = min(risk_budget / unit_risk, max_qty_by_position_notional, max_qty_by_total_notional, max_vol)
        qty = _round_down_step(raw_qty, vol_step)
        while qty >= min_vol and qty * unit_risk > risk_budget + 1e-10:
            qty = _round_down_step(qty - vol_step, vol_step)
        if qty < min_vol:
            return reject("POSITION_BELOW_MINIMUM_SIZE", raw_quantity=raw_qty, minimum=min_vol, risk_budget=risk_budget)
        modeled_stop_loss = qty * (unit_stop + unit_fees + unit_slippage + unit_funding)
        modeled_cost = qty * (unit_fees + unit_slippage + unit_funding)
        if modeled_cost / risk_budget > self.config.max_cost_to_risk:
            return reject("COST_TO_RISK_EXCEEDS_CAP", modeled_cost=modeled_cost, risk_budget=risk_budget,
                          cost_to_risk=modeled_cost / risk_budget)
        current_open_risk = sum(float(p.get("risk_amount", 0.0)) for p in open_positions)
        if current_open_risk + modeled_stop_loss > equity * self.config.max_open_risk_fraction:
            return reject("MAX_OPEN_RISK_EXCEEDED", open_risk=current_open_risk, new_risk=modeled_stop_loss)
        notional = qty * unit_notional
        return RiskDecision(True, "APPROVED_FOR_PAPER_ONLY", qty, risk_budget, modeled_stop_loss, modeled_cost,
                            notional, {"contract_size": contract_size, "vol_step": vol_step, "min_vol": min_vol,
                                       "tick_size": tick_size, "fee_rate_per_side": fee_rate,
                                       "slippage_rate_per_side": slip, "unit_risk": unit_risk})


def contract_risk_tier_terms(
    spec: Mapping[str, Any],
    *,
    direction: str,
    quantity: float,
    notional: float,
    base_mmr: float,
    contract_max_leverage: int,
) -> dict[str, Any]:
    """Resolve the MEXC public contract risk tier when its metadata exposes it.

    The private account ``risk_limit`` endpoint is deliberately not called here.
    If public contract metadata has no tier schedule, the documented base MMR is
    returned with status ``BASE_MMR_FALLBACK`` so reports can disclose the gap.
    """
    risk_type = str(spec.get("riskLimitType", "BY_VOLUME")).upper()
    basis = float(notional) if risk_type == "BY_VALUE" else float(quantity)
    mode = str(spec.get("riskLimitMode", "")).upper()

    def number(source: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
        for key in keys:
            try:
                value = float(source.get(key))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                return value
        return default

    if mode == "CUSTOM":
        custom = spec.get("riskLimitCustom")
        if not isinstance(custom, list) or not custom:
            return {"status": "BASE_MMR_FALLBACK", "mmr": base_mmr,
                    "max_leverage": contract_max_leverage, "tier": None}
        valid = [row for row in custom if isinstance(row, Mapping) and number(row, "maxVol") > 0]
        valid.sort(key=lambda row: number(row, "maxVol"))
        selected = next((row for row in valid if basis <= number(row, "maxVol")), None)
        if selected is None:
            return {"status": "OVER_LIMIT", "mmr": base_mmr, "max_leverage": 0, "tier": None}
        mmr = number(selected, "mmr", default=base_mmr)
        imr = number(selected, "imr", default=0.0)
        tier_leverage = int(number(selected, "maxLeverage", default=contract_max_leverage))
        if imr > 0:
            tier_leverage = min(tier_leverage, max(1, int(math.floor(1.0 / imr))))
        return {"status": "CONTRACT_METADATA_TIER", "mmr": mmr,
                "max_leverage": max(1, min(contract_max_leverage, tier_leverage)),
                "tier": int(number(selected, "level", default=0)) or None}

    if mode == "INCREASE":
        side = "Long" if direction.upper() == "LONG" else "Short"
        split_sides = int(number(spec, "riskLongShortSwitch", default=0)) == 1
        base_keys = (f"riskBaseVol{side}", "riskBaseVol") if split_sides else ("riskBaseVol",)
        increment_keys = (f"riskIncrVol{side}", "riskIncrVol") if split_sides else ("riskIncrVol",)
        base_volume = next((number(spec, key) for key in base_keys if number(spec, key) > 0), 0.0)
        increment_volume = next((number(spec, key) for key in increment_keys if number(spec, key) > 0), 0.0)
        level_limit = int(number(spec, "riskLevelLimit", default=0))
        if base_volume <= 0 or level_limit <= 0:
            return {"status": "BASE_MMR_FALLBACK", "mmr": base_mmr,
                    "max_leverage": contract_max_leverage, "tier": None}
        if basis <= base_volume:
            tier = 1
        elif increment_volume > 0:
            tier = int(math.ceil((basis - base_volume) / increment_volume)) + 1
        else:
            return {"status": "OVER_LIMIT", "mmr": base_mmr, "max_leverage": 0, "tier": None}
        if tier > level_limit:
            return {"status": "OVER_LIMIT", "mmr": base_mmr, "max_leverage": 0, "tier": tier}
        mmr_increment = max(0.0, number(spec, "riskIncrMmr", default=0.0))
        imr_base = max(0.0, number(spec, "initialMarginRate", default=0.0))
        imr_increment = max(0.0, number(spec, "riskIncrImr", default=0.0))
        tier_imr = imr_base + (tier - 1) * imr_increment if imr_base > 0 else 0.0
        tier_leverage = contract_max_leverage
        if tier_imr > 0:
            tier_leverage = min(tier_leverage, max(1, int(math.floor(1.0 / tier_imr))))
        return {"status": "CONTRACT_METADATA_TIER", "mmr": base_mmr + (tier - 1) * mmr_increment,
                "max_leverage": tier_leverage, "tier": tier}

    return {"status": "BASE_MMR_FALLBACK", "mmr": base_mmr,
            "max_leverage": contract_max_leverage, "tier": None}
