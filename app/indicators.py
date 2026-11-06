from __future__ import annotations

from collections.abc import Sequence


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """EMA with an SMA seed, then alpha=2/(period+1)."""
    if period < 1:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    alpha = 2.0 / (period + 1)
    previous = seed
    for i in range(period, len(values)):
        previous = (values[i] * alpha) + (previous * (1.0 - alpha))
        result[i] = previous
    return result


def rsi_wilder(values: Sequence[float], period: int) -> list[float | None]:
    """Wilder RSI; initial average gain/loss is the arithmetic N-change mean."""
    if period < 1:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def value(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        if gain == 0:
            return 0.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain, loss = max(change, 0.0), max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[i] = value(avg_gain, avg_loss)
    return result
