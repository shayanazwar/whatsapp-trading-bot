"""Small deterministic indicator functions shared by analysis and strategy code."""
from __future__ import annotations

from math import isfinite
from typing import Sequence


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be > 0")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(float(x) for x in values[:period]) / period
    if not isfinite(seed):
        return result
    result[period - 1] = seed
    alpha = 2.0 / (period + 1)
    current = seed
    for i in range(period, len(values)):
        current = alpha * float(values[i]) + (1.0 - alpha) * current
        result[i] = current
    return result


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("OHLC sequences must have equal length")
    out: list[float] = []
    previous_close: float | None = None
    for high, low, close in zip(highs, lows, closes):
        tr = high - low if previous_close is None else max(
            high - low, abs(high - previous_close), abs(low - previous_close)
        )
        out.append(float(tr))
        previous_close = float(close)
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[float | None]:
    """Simple mean of period true ranges, fixed for reproducible research in this version."""
    if period <= 0:
        raise ValueError("period must be > 0")
    tr = true_ranges(highs, lows, closes)
    out: list[float | None] = [None] * len(tr)
    for i in range(period - 1, len(tr)):
        out[i] = sum(tr[i - period + 1 : i + 1]) / period
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for a, b in zip(closes, closes[1:]):
        delta = float(b) - float(a)
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    def calc(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)
    out[period] = calc(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = calc(avg_gain, avg_loss)
    return out


def confirmed_pivots(highs: Sequence[float], lows: Sequence[float], left_right: int = 2) -> tuple[list[int], list[int]]:
    """Return pivot indices; consumers must only use a pivot after right-side bars close."""
    if left_right < 1:
        raise ValueError("left_right must be >= 1")
    highs_idx: list[int] = []
    lows_idx: list[int] = []
    for i in range(left_right, len(highs) - left_right):
        left_h = highs[i - left_right : i]
        right_h = highs[i + 1 : i + left_right + 1]
        left_l = lows[i - left_right : i]
        right_l = lows[i + 1 : i + left_right + 1]
        if all(highs[i] > v for v in (*left_h, *right_h)):
            highs_idx.append(i)
        if all(lows[i] < v for v in (*left_l, *right_l)):
            lows_idx.append(i)
    return highs_idx, lows_idx
