"""Shared exchange price/quantity precision helpers."""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def round_trigger_to_tick(price: float, tick: float, direction: str, order_type: str) -> float:
    """Round a stop/target trigger conservatively to a contract price tick.

    Long stops and short targets round away from accidental early exits; long targets
    and short stops round toward the more conservative executable price. Decimal
    arithmetic avoids binary-float boundary errors (for example, 0.3 / 0.1).
    """
    if tick <= 0:
        return price
    units = Decimal(str(price)) / Decimal(str(tick))
    if direction == "LONG":
        rounding = ROUND_CEILING if order_type == "STOP" else ROUND_FLOOR
    else:
        rounding = ROUND_FLOOR if order_type == "STOP" else ROUND_CEILING
    rounded_units = units.to_integral_value(rounding=rounding)
    return max(tick, float(rounded_units * Decimal(str(tick))))
