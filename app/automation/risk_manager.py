from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP


MIN_RR = 2.00
MIN_SL_ATR = 1.00
MAX_SL_ATR = 3.50
MIN_TP_ATR = 2.00
DEFAULT_COST_BUFFER_PCT = 0.0015  # 0.15% round-trip conservative buffer


@dataclass(frozen=True)
class TradePlan:
    side: str
    entry: float
    stop_loss: float
    tp: float
    rr: float

    # Legacy read-only aliases keep old database/test integrations from breaking;
    # the strategy itself has exactly one take-profit level.
    @property
    def tp1(self) -> float:
        return self.tp

    @property
    def tp2(self) -> float:
        return self.tp


def _finite_positive(value: float) -> bool:
    return math.isfinite(float(value)) and float(value) > 0.0


def validate_levels(plan: TradePlan, min_rr: float = MIN_RR) -> tuple[bool, str]:
    values = (plan.entry, plan.stop_loss, plan.tp, plan.rr)
    if not all(_finite_positive(value) for value in values):
        return False, "Trade levels and RR must be finite and positive"

    side = str(plan.side).upper()
    if side == "LONG":
        if not (plan.stop_loss < plan.entry < plan.tp):
            return False, "LONG levels are not ordered SL < Entry < TP"
    elif side == "SHORT":
        if not (plan.tp < plan.entry < plan.stop_loss):
            return False, "SHORT levels are not ordered TP < Entry < SL"
    else:
        return False, "Unknown trade side"

    try:
        required_rr = max(MIN_RR, float(min_rr))
    except (TypeError, ValueError):
        return False, "Minimum RR must be numeric"
    if not math.isfinite(required_rr) or required_rr <= 0:
        return False, "Minimum RR must be finite and positive"
    if plan.rr + 1e-12 < required_rr:
        return False, f"RR {plan.rr:.2f} is below minimum {required_rr:.2f}"
    return True, "OK"


def validate_atr_stop_distance(
    *,
    entry: float,
    stop_loss: float,
    atr: float,
    min_atr: float = MIN_SL_ATR,
    max_atr: float = MAX_SL_ATR,
) -> tuple[bool, str]:
    if not all(_finite_positive(value) for value in (entry, stop_loss, atr)):
        return False, "Invalid entry, stop-loss, or ATR"

    try:
        min_atr = float(min_atr)
        max_atr = float(max_atr)
    except (TypeError, ValueError):
        return False, "ATR bounds must be numeric"

    if not (
        math.isfinite(min_atr)
        and math.isfinite(max_atr)
        and 0.0 <= min_atr <= max_atr
    ):
        return False, "ATR bounds are invalid"

    distance = abs(float(entry) - float(stop_loss))
    atr_multiple = distance / float(atr)

    if atr_multiple < min_atr:
        return False, f"SL distance {atr_multiple:.2f} ATR is below minimum {min_atr:.2f}"
    if atr_multiple > max_atr:
        return False, f"SL distance {atr_multiple:.2f} ATR exceeds maximum {max_atr:.2f}"

    return True, "OK"


def quantize_to_step(
    value: float,
    step: float,
    *,
    mode: str = "down",
) -> float:
    if not _finite_positive(value) or not _finite_positive(step):
        return 0.0

    normalized_mode = str(mode).lower()
    if normalized_mode == "down":
        rounding = ROUND_DOWN
    elif normalized_mode == "up":
        rounding = ROUND_UP
    else:
        raise ValueError("Quantization mode must be 'down' or 'up'")

    value_d = Decimal(str(value))
    step_d = Decimal(str(step))
    units = (value_d / step_d).to_integral_value(rounding=rounding)
    result = float(units * step_d)
    return result if math.isfinite(result) else 0.0


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
    values = (
        risk_amount_usdt,
        entry,
        stop_loss,
        contract_size,
        vol_unit,
    )
    if not all(_finite_positive(value) for value in values):
        raise ValueError("Risk, price, contract size, and volume unit must be finite and positive")

    if not math.isfinite(float(min_vol)) or min_vol < 0:
        raise ValueError("Minimum volume must be finite and non-negative")
    if not math.isfinite(float(max_vol)) or max_vol < 0:
        raise ValueError("Maximum volume must be finite and non-negative")
    if max_vol > 0 and max_vol < min_vol:
        raise ValueError("Maximum volume cannot be below minimum volume")
    if not math.isfinite(float(cost_buffer_pct)) or cost_buffer_pct < 0:
        raise ValueError("Cost buffer must be finite and non-negative")

    stop_distance = abs(float(entry) - float(stop_loss))
    if stop_distance <= 0 or not math.isfinite(stop_distance):
        raise ValueError("Entry and stop-loss must be finite and different")

    effective_risk = float(risk_amount_usdt) / (1.0 + float(cost_buffer_pct))
    per_contract_risk = stop_distance * float(contract_size)
    raw_quantity = effective_risk / per_contract_risk

    if not math.isfinite(raw_quantity) or raw_quantity <= 0:
        raise ValueError("Calculated raw quantity is invalid")

    quantity = quantize_to_step(raw_quantity, float(vol_unit), mode="down")
    if quantity <= 0:
        raise ValueError("Calculated quantity is zero")
    if quantity < float(min_vol):
        raise ValueError("Calculated quantity is below the contract minimum")

    if max_vol > 0 and quantity > float(max_vol):
        quantity = quantize_to_step(float(max_vol), float(vol_unit), mode="down")

    if quantity <= 0 or (min_vol > 0 and quantity < min_vol):
        raise ValueError("Contract volume constraints make the requested risk size impossible")

    return quantity


def calculate_risk_amount(
    balance_usdt: float,
    risk_pct: float,
    *,
    max_risk_usdt: float | None = None,
) -> float:
    if not _finite_positive(balance_usdt):
        raise ValueError("Balance must be finite and positive")
    if not _finite_positive(risk_pct):
        raise ValueError("Risk percentage must be finite and positive")

    risk_amount = float(balance_usdt) * float(risk_pct) / 100.0

    if max_risk_usdt is not None:
        if not _finite_positive(max_risk_usdt):
            raise ValueError("Maximum risk must be finite and positive")
        risk_amount = min(risk_amount, float(max_risk_usdt))

    if not _finite_positive(risk_amount):
        raise ValueError("Calculated risk amount is invalid")

    return risk_amount


def calculate_rr(
    *,
    side: str,
    entry: float,
    stop_loss: float,
    target: float,
) -> float:
    if not all(_finite_positive(value) for value in (entry, stop_loss, target)):
        raise ValueError("Entry, stop-loss and target must be finite and positive")

    risk = abs(float(entry) - float(stop_loss))
    if risk <= 0 or not math.isfinite(risk):
        raise ValueError("Entry and stop-loss must differ")

    normalized_side = str(side).upper()
    if normalized_side == "LONG":
        reward = float(target) - float(entry)
    elif normalized_side == "SHORT":
        reward = float(entry) - float(target)
    else:
        raise ValueError("Unknown trade side")

    if reward <= 0 or not math.isfinite(reward):
        raise ValueError("Target must be profitable for the trade side")

    rr = reward / risk
    if not math.isfinite(rr) or rr <= 0:
        raise ValueError("Calculated RR is invalid")
    return float(rr)



def calculate_rr_after_costs(
    *,
    side: str,
    entry: float,
    stop_loss: float,
    target: float,
    round_trip_cost_pct: float = DEFAULT_COST_BUFFER_PCT,
) -> float:
    """Conservative RR after a round-trip cost allowance.

    Costs are expressed as a fraction of entry price and are deducted from the
    reward while added to the risk denominator. This prevents a geometrically
    attractive trade from passing only because costs are ignored.
    """
    gross_rr = calculate_rr(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        target=target,
    )
    cost_pct = max(0.0, float(round_trip_cost_pct))
    cost_price = float(entry) * cost_pct
    risk = abs(float(entry) - float(stop_loss))
    if risk <= 0:
        raise ValueError("Entry and stop-loss must differ")
    reward = (
        float(target) - float(entry)
        if str(side).upper() == "LONG"
        else float(entry) - float(target)
    )
    net_reward = reward - cost_price
    net_risk = risk + cost_price
    if net_reward <= 0:
        return 0.0
    rr = net_reward / net_risk
    return float(rr) if math.isfinite(rr) else 0.0


def build_trade_plan(
    *,
    side: str,
    entry: float,
    stop_loss: float,
    tp: float,
    min_rr: float = MIN_RR,
) -> TradePlan:
    rr = calculate_rr(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        target=tp,
    )
    plan = TradePlan(
        side=str(side).upper(),
        entry=float(entry),
        stop_loss=float(stop_loss),
        tp=float(tp),
        rr=rr,
    )
    valid, reason = validate_levels(plan, min_rr=max(MIN_RR, float(min_rr)))
    if not valid:
        raise ValueError(reason)
    return plan
