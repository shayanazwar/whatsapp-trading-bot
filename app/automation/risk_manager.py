from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP


@dataclass(frozen=True)
class TradePlan:
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    rr: float


def validate_levels(plan: TradePlan, min_rr: float) -> tuple[bool, str]:
    values = [plan.entry, plan.stop_loss, plan.tp1, plan.tp2, plan.rr]
    if not all(value > 0 for value in values):
        return False, "Trade levels must all be positive"

    if plan.side == "LONG":
        if not (plan.stop_loss < plan.entry < plan.tp1 < plan.tp2):
            return False, "LONG levels are not ordered SL < Entry < TP1 < TP2"
    elif plan.side == "SHORT":
        if not (plan.tp2 < plan.tp1 < plan.entry < plan.stop_loss):
            return False, "SHORT levels are not ordered TP2 < TP1 < Entry < SL"
    else:
        return False, "Unknown trade side"

    if plan.rr < min_rr:
        return False, f"RR {plan.rr:.2f} is below minimum {min_rr:.2f}"
    return True, "OK"


def quantize_to_step(value: float, step: float, *, mode: str = "down") -> float:
    if value <= 0 or step <= 0:
        return 0.0
    value_d = Decimal(str(value))
    step_d = Decimal(str(step))
    rounding = ROUND_DOWN if mode == "down" else ROUND_UP
    units = (value_d / step_d).to_integral_value(rounding=rounding)
    return float(units * step_d)


def calculate_contract_quantity(
    risk_amount_usdt: float,
    entry: float,
    stop_loss: float,
    contract_size: float,
    vol_unit: float,
    min_vol: float,
    max_vol: float,
) -> float:
    if risk_amount_usdt <= 0 or entry <= 0 or stop_loss <= 0 or contract_size <= 0:
        raise ValueError("Invalid inputs for position sizing")

    per_contract_risk = abs(entry - stop_loss) * contract_size
    if per_contract_risk <= 0:
        raise ValueError("Entry and stop-loss must be different")

    raw_quantity = risk_amount_usdt / per_contract_risk
    quantity = quantize_to_step(raw_quantity, vol_unit, mode="down")

    if quantity < min_vol:
        raise ValueError("Calculated quantity is below the contract minimum")
    if max_vol > 0 and quantity > max_vol:
        quantity = quantize_to_step(max_vol, vol_unit, mode="down")
    if quantity <= 0:
        raise ValueError("Calculated quantity is zero")
    return quantity
