from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any


MIN_RR = 2.0
MIN_SL_ATR = 0.50
MAX_SL_ATR = 1.80

# Conservative allowance for trading costs/slippage during sizing.
DEFAULT_COST_BUFFER_PCT = 0.0015  # 0.15%


@dataclass(frozen=True)
class TradePlan:
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    rr: float


def validate_levels(
    plan: TradePlan,
    min_rr: float = MIN_RR,
) -> tuple[bool, str]:
    values = [
        plan.entry,
        plan.stop_loss,
        plan.tp1,
        plan.tp2,
        plan.rr,
    ]

    if not all(value > 0 for value in values):
        return False, "Trade levels must all be positive"

    side = plan.side.upper()

    if side == "LONG":
        if not (
            plan.stop_loss
            < plan.entry
            < plan.tp1
            < plan.tp2
        ):
            return (
                False,
                "LONG levels are not ordered "
                "SL < Entry < TP1 < TP2",
            )

    elif side == "SHORT":
        if not (
            plan.tp2
            < plan.tp1
            < plan.entry
            < plan.stop_loss
        ):
            return (
                False,
                "SHORT levels are not ordered "
                "TP2 < TP1 < Entry < SL",
            )

    else:
        return False, "Unknown trade side"

    required_rr = max(MIN_RR, float(min_rr))

    if plan.rr < required_rr:
        return (
            False,
            f"RR {plan.rr:.2f} is below minimum "
            f"{required_rr:.2f}",
        )

    return True, "OK"


def validate_atr_stop_distance(
    *,
    entry: float,
    stop_loss: float,
    atr: float,
    min_atr: float = MIN_SL_ATR,
    max_atr: float = MAX_SL_ATR,
) -> tuple[bool, str]:
    if entry <= 0 or stop_loss <= 0 or atr <= 0:
        return False, "Invalid entry, stop-loss, or ATR"

    distance = abs(entry - stop_loss)
    atr_multiple = distance / atr

    if atr_multiple < min_atr:
        return (
            False,
            f"SL distance {atr_multiple:.2f} ATR "
            f"is below minimum {min_atr:.2f}",
        )

    if atr_multiple > max_atr:
        return (
            False,
            f"SL distance {atr_multiple:.2f} ATR "
            f"exceeds maximum {max_atr:.2f}",
        )

    return True, "OK"


def quantize_to_step(
    value: float,
    step: float,
    *,
    mode: str = "down",
) -> float:
    if value <= 0 or step <= 0:
        return 0.0

    value_d = Decimal(str(value))
    step_d = Decimal(str(step))

    rounding = (
        ROUND_DOWN
        if mode == "down"
        else ROUND_UP
    )

    units = (
        value_d / step_d
    ).to_integral_value(rounding=rounding)

    return float(units * step_d)


def calculate_contract_quantity(
    risk_amount_usdt: float,
    entry: float,
    stop_loss: float,
    contract_size: float,
    vol_unit: float,
    min_vol: float,
    max_vol: float,
    *,
    cost_buffer_pct: float = DEFAULT_COST_BUFFER_PCT,
) -> float:
    if risk_amount_usdt <= 0:
        raise ValueError("Risk amount must be positive")

    if entry <= 0 or stop_loss <= 0:
        raise ValueError("Entry and stop-loss must be positive")

    if contract_size <= 0:
        raise ValueError("Contract size must be positive")

    if vol_unit <= 0:
        raise ValueError("Volume unit must be positive")

    if min_vol < 0:
        raise ValueError("Minimum volume cannot be negative")

    if max_vol < 0:
        raise ValueError("Maximum volume cannot be negative")

    if cost_buffer_pct < 0:
        raise ValueError("Cost buffer cannot be negative")

    stop_distance = abs(entry - stop_loss)

    if stop_distance <= 0:
        raise ValueError(
            "Entry and stop-loss must be different"
        )

    # Increase planned risk slightly to leave room for
    # fees/slippage rather than sizing right at the limit.
    effective_risk = risk_amount_usdt / (
        1.0 + cost_buffer_pct
    )

    per_contract_risk = (
        stop_distance * contract_size
    )

    if per_contract_risk <= 0:
        raise ValueError(
            "Per-contract risk must be positive"
        )

    raw_quantity = (
        effective_risk / per_contract_risk
    )

    quantity = quantize_to_step(
        raw_quantity,
        vol_unit,
        mode="down",
    )

    if quantity <= 0:
        raise ValueError(
            "Calculated quantity is zero"
        )

    if quantity < min_vol:
        raise ValueError(
            "Calculated quantity is below "
            "the contract minimum"
        )

    if max_vol > 0 and quantity > max_vol:
        quantity = quantize_to_step(
            max_vol,
            vol_unit,
            mode="down",
        )

    if quantity <= 0:
        raise ValueError(
            "Calculated quantity is zero"
        )

    return quantity


def calculate_risk_amount(
    balance_usdt: float,
    risk_pct: float,
    *,
    max_risk_usdt: float | None = None,
) -> float:
    if balance_usdt <= 0:
        raise ValueError(
            "Balance must be positive"
        )

    if risk_pct <= 0:
        raise ValueError(
            "Risk percentage must be positive"
        )

    risk_amount = (
        balance_usdt * risk_pct / 100.0
    )

    if max_risk_usdt is not None:
        if max_risk_usdt <= 0:
            raise ValueError(
                "Maximum risk must be positive"
            )

        risk_amount = min(
            risk_amount,
            max_risk_usdt,
        )

    if risk_amount <= 0:
        raise ValueError(
            "Calculated risk amount is zero"
        )

    return risk_amount


def calculate_rr(
    *,
    side: str,
    entry: float,
    stop_loss: float,
    target: float,
) -> float:
    if entry <= 0 or stop_loss <= 0 or target <= 0:
        raise ValueError(
            "Entry, stop-loss and target "
            "must be positive"
        )

    risk = abs(entry - stop_loss)

    if risk <= 0:
        raise ValueError(
            "Entry and stop-loss must differ"
        )

    side = side.upper()

    if side == "LONG":
        reward = target - entry
    elif side == "SHORT":
        reward = entry - target
    else:
        raise ValueError("Unknown trade side")

    if reward <= 0:
        raise ValueError(
            "Target must be profitable for the trade side"
        )

    return reward / risk


def build_trade_plan(
    *,
    side: str,
    entry: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    min_rr: float = MIN_RR,
) -> TradePlan:
    rr = calculate_rr(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        target=tp2,
    )

    plan = TradePlan(
        side=side.upper(),
        entry=float(entry),
        stop_loss=float(stop_loss),
        tp1=float(tp1),
        tp2=float(tp2),
        rr=float(rr),
    )

    valid, reason = validate_levels(
        plan,
        min_rr=max(MIN_RR, min_rr),
    )

    if not valid:
        raise ValueError(reason)

    return plan
