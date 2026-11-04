from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # Explicitly handle zero-loss and zero-gain edge cases: monotonic gains -> 100,
    # monotonic losses -> 0, and a perfectly flat series -> 50.
    result = result.mask((loss == 0) & (gain > 0), 100.0)
    result = result.mask((gain == 0) & (loss > 0), 0.0)
    result = result.mask((gain == 0) & (loss == 0), 50.0)
    return result


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = frame["close"].shift(1)
    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev_close).abs(),
        (frame["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def enrich(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy().sort_values("timestamp").reset_index(drop=True)
    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["rsi14"] = rsi(out["close"], 14)
    out["atr14"] = atr(out, 14)
    out["volume_sma20"] = out["volume"].rolling(20, min_periods=10).mean()
    out["body"] = (out["close"] - out["open"]).abs()
    out["body_atr"] = out["body"] / out["atr14"].replace(0, np.nan)
    return out


def confirmed_swings(frame: pd.DataFrame, left: int = 2, right: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """A pivot is considered known only after its right-hand confirmation bars have closed."""
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    h = frame["high"].to_numpy(dtype=float)
    l = frame["low"].to_numpy(dtype=float)
    for i in range(left, len(frame) - right):
        if h[i] >= np.max(h[i-left:i+right+1]) and np.count_nonzero(h[i] == h[i-left:i+right+1]) == 1:
            highs.append((i + right, float(h[i])))
        if l[i] <= np.min(l[i-left:i+right+1]) and np.count_nonzero(l[i] == l[i-left:i+right+1]) == 1:
            lows.append((i + right, float(l[i])))
    return highs, lows


def structure_state(frame: pd.DataFrame) -> dict:
    if len(frame) < 12:
        return {"trend": "UNKNOWN", "bos": None, "choch": None, "swing_high": None, "swing_low": None}
    highs, lows = confirmed_swings(frame)
    trend = "NEUTRAL"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            trend = "BULLISH"
        elif lh and ll:
            trend = "BEARISH"
    last_close = float(frame.iloc[-1]["close"])
    previous_close = float(frame.iloc[-2]["close"])
    # BOS is a crossing event, not a persistent state while price remains beyond
    # an old swing. The swing must already be confirmed by the current bar.
    latest_high = highs[-1] if highs else None
    latest_low = lows[-1] if lows else None
    last_high = latest_high[1] if latest_high else None
    last_low = latest_low[1] if latest_low else None
    high_is_confirmed = latest_high is not None and latest_high[0] <= len(frame) - 1
    low_is_confirmed = latest_low is not None and latest_low[0] <= len(frame) - 1
    bullish_cross = bool(high_is_confirmed and last_high is not None and previous_close <= last_high < last_close)
    bearish_cross = bool(low_is_confirmed and last_low is not None and previous_close >= last_low > last_close)
    bos = "BULLISH" if bullish_cross else ("BEARISH" if bearish_cross else None)
    choch = None
    if trend == "BULLISH" and bos == "BEARISH":
        choch = "BEARISH"
    elif trend == "BEARISH" and bos == "BULLISH":
        choch = "BULLISH"
    return {"trend": trend, "bos": bos, "choch": choch, "swing_high": last_high, "swing_low": last_low}
