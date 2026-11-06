from __future__ import annotations

import math

import pandas as pd

from rslr.indicators import ema


BIAS_LONG = "LONG_BIAS"
BIAS_SHORT = "SHORT_BIAS"
BIAS_BOTH = "BOTH"
BIAS_NONE = "NO_TRADE"


def btc_regime(daily: pd.DataFrame) -> str:
    """Daily BTC regime using completed daily bars only."""
    if len(daily) < 40:
        return BIAS_NONE
    close = daily.close
    fast = ema(close, 20)
    slow = ema(close, 35)
    last = len(daily) - 1
    if pd.isna(slow.iloc[last]) or pd.isna(slow.iloc[last - 5]):
        return BIAS_NONE
    daily_range = ((daily.high - daily.low) / daily.close).tail(14).median()
    if not math.isfinite(float(daily_range)) or daily_range > 0.12:
        return BIAS_NONE
    if close.iloc[last] > slow.iloc[last] and fast.iloc[last] > slow.iloc[last] and slow.iloc[last] > slow.iloc[last - 5]:
        return BIAS_LONG
    if close.iloc[last] < slow.iloc[last] and fast.iloc[last] < slow.iloc[last] and slow.iloc[last] < slow.iloc[last - 5]:
        return BIAS_SHORT
    return BIAS_BOTH


def relative_strength(symbol_12h: pd.DataFrame, btc_12h: pd.DataFrame, lookback: int = 20) -> float | None:
    """Return volatility-adjusted 12H relative strength versus BTC."""
    if len(symbol_12h) < lookback + 1 or len(btc_12h) < lookback + 1:
        return None
    sym = symbol_12h.close.pct_change().dropna().tail(lookback)
    btc = btc_12h.close.pct_change().dropna().tail(lookback)
    if len(sym) != lookback or len(btc) != lookback:
        return None
    excess = float(sym.sum() - btc.sum())
    volatility = float(sym.std(ddof=0))
    if not math.isfinite(excess) or not math.isfinite(volatility):
        return None
    return excess / max(volatility, 1e-6)



def four_hour_context(four_hour: pd.DataFrame) -> dict[str, float | str]:
    """Summarize completed 4H trend and recent structure without hard-gating entries."""
    if len(four_hour) < 25:
        return {"bias": "UNKNOWN", "structure": "UNKNOWN", "ema21": 0.0, "ema50": 0.0}
    close = four_hour.close
    fast = ema(close, 21)
    slow = ema(close, 50)
    last = len(four_hour) - 1
    fast_value = float(fast.iloc[last]) if pd.notna(fast.iloc[last]) else 0.0
    slow_value = float(slow.iloc[last]) if pd.notna(slow.iloc[last]) else 0.0
    if fast_value > slow_value and close.iloc[last] > fast_value and fast.iloc[last] > fast.iloc[last - 3]:
        bias = "UPTREND"
    elif fast_value < slow_value and close.iloc[last] < fast_value and fast.iloc[last] < fast.iloc[last - 3]:
        bias = "DOWNTREND"
    else:
        bias = "NEUTRAL"
    prior = four_hour.iloc[-21:-1]
    if len(prior) < 20:
        structure = "UNKNOWN"
    elif close.iloc[last] > prior.high.max():
        structure = "BOS_UP"
    elif close.iloc[last] < prior.low.min():
        structure = "BOS_DOWN"
    else:
        structure = "RANGE"
    return {"bias": bias, "structure": structure, "ema21": fast_value, "ema50": slow_value}


def funding_context(funding: pd.DataFrame, at: pd.Timestamp) -> dict[str, float | str]:
    rows = funding[funding.timestamp <= at].tail(30) if not funding.empty else funding
    if rows.empty:
        return {"change": 0.0, "zscore": 0.0, "state": "UNAVAILABLE"}
    values = rows.funding_rate.astype(float)
    current = float(values.iloc[-1])
    previous = float(values.iloc[-2]) if len(values) > 1 else current
    std = float(values.std(ddof=0)) if len(values) > 1 else 0.0
    zscore = (current - float(values.mean())) / std if std > 1e-12 else 0.0
    state = "EXTREME_POSITIVE" if zscore >= 2 else "EXTREME_NEGATIVE" if zscore <= -2 else "NORMAL"
    return {"change": current - previous, "zscore": zscore, "state": state}

def funding_is_usable(funding: pd.DataFrame, at: pd.Timestamp, max_age_hours: int = 36) -> float | None:
    if funding.empty:
        return None
    rows = funding[funding.timestamp <= at]
    if rows.empty:
        return None
    last = rows.iloc[-1]
    age = at - last.timestamp
    if age > pd.Timedelta(hours=max_age_hours):
        return None
    return float(last.funding_rate)


def funding_allows_side(side: str, rate: float, extreme: float = 0.0015) -> bool:
    """Avoid trades whose entry side pays an extreme funding rate."""
    if side == "LONG" and rate >= extreme:
        return False
    if side == "SHORT" and rate <= -extreme:
        return False
    return True
