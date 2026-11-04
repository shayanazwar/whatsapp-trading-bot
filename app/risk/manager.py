from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import math


@dataclass(frozen=True)
class RiskPlan:
    risk_budget_usdt: float
    contracts: float
    base_quantity: float
    notional_usdt: float
    estimated_stop_loss_usdt: float
    initial_margin_estimate_usdt: float
    liquidation_stress_price: float | None
    leverage: int
    notes: list[str]

    def as_dict(self):
        return asdict(self)


def quantize_price(price: float, tick_size: float, mode: str = "nearest") -> float:
    """Round a price to the exchange tick; modes: floor, ceil, nearest."""
    if not math.isfinite(price) or price <= 0:
        raise ValueError("price must be finite and positive")
    if not math.isfinite(tick_size) or tick_size <= 0:
        raise ValueError("tick_size must be finite and positive")
    rounding = {"floor": ROUND_FLOOR, "ceil": ROUND_CEILING, "nearest": ROUND_HALF_UP}.get(mode)
    if rounding is None:
        raise ValueError("mode must be floor, ceil or nearest")
    p, tick = Decimal(str(price)), Decimal(str(tick_size))
    units = (p / tick).to_integral_value(rounding=rounding)
    return float(units * tick)


def _floor_step(value: float, step: float) -> float:
    units = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * Decimal(str(step)))



def resolve_contract_risk_limits(contract: dict) -> dict:
    """Resolve conservative contract caps and first applicable MEXC margin tier.

    Sizing is capped at the first risk tier so it never silently assumes the
    lower-tier margin schedule applies to a larger position.
    """
    volume_caps = [
        float(value) for value in (contract.get("maxVol"), contract.get("limitMaxVol"))
        if value is not None and math.isfinite(float(value)) and float(value) > 0
    ]
    max_contracts = min(volume_caps) if volume_caps else None
    risk_mode = str(contract.get("riskLimitMode") or "NORMAL").upper()
    max_leverage = int(contract.get("maxLeverage") or 0) or None
    initial_rate = contract.get("initialMarginRate")
    maintenance_rate = contract.get("maintenanceMarginRate")
    tier_source = "CONTRACT_BASE_FIELDS"

    if risk_mode == "CUSTOM":
        tiers = []
        for tier in contract.get("riskLimitCustom") or []:
            try:
                cap = float(tier["maxVol"])
                imr = float(tier["imr"])
                mmr = float(tier["mmr"])
                tier_lev = int(tier["maxLeverage"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if (math.isfinite(cap) and cap > 0 and math.isfinite(imr) and 0 < imr < 1
                    and math.isfinite(mmr) and 0 <= mmr < 1 and tier_lev >= 1):
                tiers.append((cap, imr, mmr, tier_lev))
        if not tiers:
            raise ValueError("CUSTOM risk-limit contract lacks valid first-tier maxVol/imr/mmr/maxLeverage")
        cap, initial_rate, maintenance_rate, tier_max_leverage = min(tiers, key=lambda item: item[0])
        max_contracts = min(max_contracts, cap) if max_contracts is not None else cap
        max_leverage = min(max_leverage, tier_max_leverage) if max_leverage is not None else tier_max_leverage
        tier_source = "CUSTOM_FIRST_RISK_TIER"
    elif risk_mode == "INCREASE":
        base_cap = contract.get("riskBaseVol")
        if base_cap is None or float(base_cap) <= 0:
            raise ValueError("INCREASE risk-limit contract lacks a valid riskBaseVol")
        max_contracts = min(max_contracts, float(base_cap)) if max_contracts is not None else float(base_cap)
        # The initial tier is bounded to riskBaseVol; require the contract's
        # published baseline margin rates instead of estimating higher tiers.
        if initial_rate is None or maintenance_rate is None:
            raise ValueError("INCREASE risk-limit contract lacks baseline initial/maintenance margin rates")
        tier_source = "INCREASE_BASE_TIER"
    elif risk_mode not in {"", "NORMAL"}:
        raise ValueError(f"Unsupported risk-limit mode: {risk_mode}")

    if max_contracts is None or not math.isfinite(float(max_contracts)) or float(max_contracts) <= 0:
        raise ValueError("Contract maximum volume/risk-tier cap is unavailable")
    if initial_rate is None or maintenance_rate is None:
        raise ValueError("Contract initial/maintenance margin rates are unavailable")
    initial_rate = float(initial_rate)
    maintenance_rate = float(maintenance_rate)
    if not 0 < initial_rate < 1 or not 0 <= maintenance_rate < 1:
        raise ValueError("Contract initial/maintenance margin rates are invalid")
    if max_leverage is None or max_leverage < 1:
        raise ValueError("Contract maximum leverage is unavailable")
    return {
        "max_contracts": float(max_contracts),
        "max_leverage": max_leverage,
        "initial_margin_rate": initial_rate,
        "maintenance_margin_rate": maintenance_rate,
        "risk_limit_mode": risk_mode or "NORMAL",
        "risk_tier_source": tier_source,
    }


def validate_portfolio_capacity(*, equity: float, committed_risk: float, proposed_risk: float,
                                max_risk_pct: float, committed_margin: float, proposed_margin: float,
                                margin_buffer_pct: float, committed_notional: float,
                                proposed_notional: float, max_notional_multiple: float) -> list[str]:
    """Return fail-closed portfolio-cap reasons for concurrent paper reservations."""
    values = (equity, committed_risk, proposed_risk, max_risk_pct, committed_margin,
              proposed_margin, margin_buffer_pct, committed_notional, proposed_notional,
              max_notional_multiple)
    if not all(math.isfinite(float(value)) for value in values):
        return ["Portfolio capacity inputs contain non-finite values"]
    if equity <= 0 or not 0 <= margin_buffer_pct < 100 or max_risk_pct <= 0 or max_notional_multiple <= 0:
        return ["Portfolio capacity configuration is invalid"]
    reasons = []
    if committed_risk + proposed_risk > equity * max_risk_pct / 100:
        reasons.append(
            f"Aggregate active paper stop-risk {committed_risk + proposed_risk:.2f} exceeds "
            f"cap {equity * max_risk_pct / 100:.2f} USDT"
        )
    margin_cap = equity * (1 - margin_buffer_pct / 100)
    if committed_margin + proposed_margin > margin_cap:
        reasons.append(
            f"Aggregate active paper margin reservation {committed_margin + proposed_margin:.2f} "
            f"exceeds usable margin {margin_cap:.2f} USDT"
        )
    notional_cap = equity * max_notional_multiple
    if committed_notional + proposed_notional > notional_cap:
        reasons.append(
            f"Aggregate active paper notional {committed_notional + proposed_notional:.2f} "
            f"exceeds cap {notional_cap:.2f} USDT"
        )
    return reasons

def size_position(*, equity: float, risk_pct: float, entry: float, stop: float, side: str,
                  contract_size: float, vol_unit: float, leverage: int,
                  taker_fee_rate: float | None, slippage_bps: float = 3,
                  maintenance_margin_rate: float | None = None,
                  initial_margin_rate: float | None = None,
                  available_margin_usdt: float | None = None,
                  margin_buffer_pct: float = 10.0,
                  min_contracts: float = 0.0,
                  max_contracts: float | None = None,
                  max_leverage: int | None = None) -> RiskPlan:
    """Size by the tightest of stop-risk, available initial margin and contract limits."""
    values = [equity, risk_pct, entry, stop, contract_size, vol_unit, slippage_bps,
              margin_buffer_pct, min_contracts]
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError("All sizing inputs must be finite")
    if equity <= 0 or not (0 < risk_pct <= 5) or entry <= 0 or stop <= 0:
        raise ValueError("Invalid equity, risk percentage or entry/stop")
    if side not in {"LONG", "SHORT"} or contract_size <= 0 or vol_unit <= 0 or leverage < 1:
        raise ValueError("Invalid side, contract size, quantity step or leverage")
    if max_leverage is not None and leverage > int(max_leverage):
        raise ValueError(f"Requested leverage {leverage} exceeds contract maximum {max_leverage}")
    if min_contracts < 0:
        raise ValueError("min_contracts cannot be negative")
    if not 0 <= margin_buffer_pct < 100:
        raise ValueError("margin_buffer_pct must be in [0, 100)")
    if taker_fee_rate is None or not math.isfinite(float(taker_fee_rate)) or taker_fee_rate < 0:
        raise ValueError("Verified contract/account taker fee is required for net risk sizing")
    if slippage_bps < 0:
        raise ValueError("slippage_bps cannot be negative")
    distance = entry - stop if side == "LONG" else stop - entry
    if distance <= 0:
        raise ValueError("Stop must be on the losing side of entry")

    risk_budget = equity * risk_pct / 100
    round_trip_cost_per_contract = contract_size * (entry + stop) * float(taker_fee_rate)
    slippage_cost_per_contract = contract_size * (entry + stop) * slippage_bps / 10_000
    loss_per_contract = distance * contract_size + round_trip_cost_per_contract + slippage_cost_per_contract
    if loss_per_contract <= 0:
        raise ValueError("Calculated per-contract loss is invalid")
    risk_limited = risk_budget / loss_per_contract

    # Exchange initial-margin rate may be stricter than 1/leverage. If it is
    # unavailable, 1/leverage is only an estimate and is labelled as such.
    if initial_margin_rate is not None:
        if not math.isfinite(float(initial_margin_rate)) or not 0 < float(initial_margin_rate) < 1:
            raise ValueError("Invalid exchange initialMarginRate")
        margin_rate = max(1.0 / leverage, float(initial_margin_rate))
    else:
        margin_rate = 1.0 / leverage
    available = equity if available_margin_usdt is None else float(available_margin_usdt)
    if not math.isfinite(available) or available < 0:
        raise ValueError("available_margin_usdt must be finite and non-negative")
    usable_margin = min(available, equity) * (1 - margin_buffer_pct / 100)
    # Reserve the opening taker fee and modeled entry slippage as well as
    # initial margin; otherwise a position can fit nominal margin but exceed
    # available funds once the opening fill is charged.
    opening_cost_per_contract = entry * contract_size * (
        float(taker_fee_rate) + slippage_bps / 10_000
    )
    margin_per_contract = entry * contract_size * margin_rate + opening_cost_per_contract
    margin_limited = usable_margin / margin_per_contract if margin_per_contract > 0 else 0.0

    contract_cap = float(max_contracts) if max_contracts is not None else math.inf
    if max_contracts is not None and not math.isfinite(contract_cap):
        raise ValueError("max_contracts must be finite when supplied; use None for no cap")
    if contract_cap <= 0:
        raise ValueError("max_contracts must be positive")
    raw_contracts = min(risk_limited, margin_limited, contract_cap)
    contracts = _floor_step(raw_contracts, vol_unit)
    if contracts <= 0 or contracts < min_contracts:
        raise ValueError(
            "Position is below the exchange minimum after applying stop-risk, margin, leverage and contract limits"
        )
    if max_contracts is not None and contracts > max_contracts + 1e-10:
        raise ValueError("Position exceeds exchange maximum contract volume")

    qty = contracts * contract_size
    notional = qty * entry
    estimated_loss = contracts * loss_per_contract
    margin = notional * margin_rate
    notes = [
        "Paper estimate only; actual fills, fees, margin tiers and liquidation can differ.",
        f"Sizing bounded by stop risk ({risk_limited:.8g}), available margin including opening costs ({margin_limited:.8g}) and contract cap ({contract_cap:.8g}) contracts.",
        f"A {margin_buffer_pct:.2f}% equity margin reserve is retained.",
    ]
    if initial_margin_rate is None:
        notes.append("Exchange initialMarginRate unavailable; initial margin uses 1/leverage estimate.")
    liq = None
    if maintenance_margin_rate is not None:
        if not math.isfinite(float(maintenance_margin_rate)) or not 0 <= float(maintenance_margin_rate) < 1:
            raise ValueError("Invalid exchange maintenanceMarginRate")
        # Only a simplified isolated linear-contract stress estimate.
        if side == "LONG":
            liq = entry * (1 - 1 / leverage) / (1 - maintenance_margin_rate)
        else:
            liq = entry * (1 + 1 / leverage) / (1 + maintenance_margin_rate)
        notes.append("Liquidation estimate is simplified; tiered/cross-margin liquidation is not simulated.")
    return RiskPlan(risk_budget, contracts, qty, notional, estimated_loss, margin, liq, leverage, notes)
