from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

from app.indicators import ema, rsi_wilder
from app.models import Candle, Direction, Signal


@dataclass(frozen=True)
class StrategyConfig:
    ema_fast: int = 6
    ema_mid: int = 12
    ema_slow: int = 21
    rsi_period: int = 9
    rsi_upper: float = 65
    rsi_lower: float = 35
    tp_move_pct: float = 0.50


def evaluate(candles: Sequence[Candle], config: StrategyConfig) -> Signal | None:
    """Evaluate the last supplied candle. Caller must supply only completed candles."""
    if len(candles) < max(config.ema_slow, config.rsi_period + 2):
        return None
    closes = [c.close for c in candles]
    fast = ema(closes, config.ema_fast)
    mid = ema(closes, config.ema_mid)
    slow = ema(closes, config.ema_slow)
    rsi = rsi_wilder(closes, config.rsi_period)
    i = len(candles) - 1
    prev = i - 1
    needed = (fast[i], mid[i], slow[i], rsi[i], rsi[prev])
    if any(x is None for x in needed):
        return None
    candle = candles[i]
    price = candle.close
    direction: Direction | None = None
    if fast[i] > mid[i] > slow[i] and price > fast[i] and price > mid[i] and price > slow[i]:
        if rsi[i] >= config.rsi_upper and rsi[i] > rsi[prev]:
            direction = "LONG"
    elif fast[i] < mid[i] < slow[i] and price < fast[i] and price < mid[i] and price < slow[i]:
        if rsi[i] <= config.rsi_lower and rsi[i] < rsi[prev]:
            direction = "SHORT"
    if direction is None:
        return None
    move = config.tp_move_pct / 100.0
    tp = price * (1.0 + move if direction == "LONG" else 1.0 - move)
    return Signal(
        direction=direction,
        candle_time=candle.time,
        price=price,
        rsi=float(rsi[i]),
        tp_price=tp,
        tp_pct=config.tp_move_pct,
        rsi_period=config.rsi_period,
        rsi_upper=config.rsi_upper,
        rsi_lower=config.rsi_lower,
    )


def signal_at(
    candles: Sequence[Candle], index: int, config: StrategyConfig,
    fast_values: Sequence[float | None], mid_values: Sequence[float | None],
    slow_values: Sequence[float | None], rsi_values: Sequence[float | None],
) -> Signal | None:
    if index <= 0 or index >= len(candles):
        return None
    fast, mid, slow = fast_values[index], mid_values[index], slow_values[index]
    current_rsi, previous_rsi = rsi_values[index], rsi_values[index - 1]
    if any(x is None for x in (fast, mid, slow, current_rsi, previous_rsi)):
        return None
    candle = candles[index]
    price = candle.close
    direction: Direction | None = None
    if fast > mid > slow and price > fast and price > mid and price > slow:
        if current_rsi >= config.rsi_upper and current_rsi > previous_rsi:
            direction = "LONG"
    elif fast < mid < slow and price < fast and price < mid and price < slow:
        if current_rsi <= config.rsi_lower and current_rsi < previous_rsi:
            direction = "SHORT"
    if direction is None:
        return None
    move = config.tp_move_pct / 100.0
    target = price * (1.0 + move if direction == "LONG" else 1.0 - move)
    return Signal(direction, candle.time, price, float(current_rsi), target, config.tp_move_pct,
                  config.rsi_period, config.rsi_upper, config.rsi_lower)
