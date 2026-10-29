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
