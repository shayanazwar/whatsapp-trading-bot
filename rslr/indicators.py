from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous = frame.close.shift(1)
    return pd.concat(
        [(frame.high - frame.low), (frame.high - previous).abs(), (frame.low - previous).abs()], axis=1
    ).max(axis=1)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(frame).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    values = 100 - (100 / (1 + rs))
    return values.fillna(50)


def donchian(frame: pd.DataFrame, period: int = 20) -> tuple[pd.Series, pd.Series]:
    # Shift by one: current signal candle is excluded from its own channel.
    return frame.high.rolling(period, min_periods=period).max().shift(1), frame.low.rolling(period, min_periods=period).min().shift(1)
