from __future__ import annotations

"""Deterministic MEXC Futures multi-timeframe signal engine.

V1.5 - full deterministic pipeline.

Pipeline:
1D context
    -> 4H regime
    -> 1H directional evidence
    -> 15M BOS + retest
    -> 5M trigger
    -> momentum / volume / volatility
    -> structural SL
    -> structural TP path
    -> RR / freshness / execution gates
    -> technical candidate

This module never places orders.
"""

from dataclasses import dataclass
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .indicators import atr, ema, rsi, volume_status
from .structure import get_structure, get_support_resistance


# ============================================================
# SETTINGS
# ============================================================

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}

TIMEFRAME_ALIASES = {
    "1M": "1m", "1MIN": "1m",
    "5M": "5m", "5MIN": "5m",
    "15M": "15m", "15MIN": "15m",
    "30M": "30m", "30MIN": "30m",
    "1H": "1h", "1HR": "1h", "1HOUR": "1h",
    "4H": "4h", "4HR": "4h", "4HOUR": "4h",
    "8H": "8h", "8HR": "8h", "8HOUR": "8h",
    "1D": "1d", "1DAY": "1d",
    "1W": "1w", "1WEEK": "1w",
}

MIN_SCORE = 82
MIN_RR = 2.0
MIN_FAMILIES = 5

MIN_SL_ATR = 0.50
MAX_SL_ATR = 1.80

MIN_ATR_PERCENTILE = 15.0
MAX_ATR_PERCENTILE = 95.0

MAX_SETUP_AGE_15M = 8
MAX_ENTRY_DISTANCE_ATR = 1.50

BOS_BUFFER_ATR = 0.08
BOS_BUFFER_PCT = 0.0004

BTC_SHOCK_ATR = 1.75

ADX_TREND_MIN = 18.0
EMA_TOLERANCE_PCT = 0.0040

MIN_TRIGGER_RVOL = 0.90
MIN_TRIGGER_BODY = 0.45

RETEST_TOLERANCE_ATR = 0.45
RETEST_PENETRATION_ATR = 0.90

MIN_TP1_R = 1.20
MIN_TP2_R = 2.00

ENGINE_VERSION = "gold-v1.5-deterministic"


# ============================================================
# CANDLE
# ============================================================

class Candle(dict):
    _legacy_keys = ("time", "open", "high", "low", "close", "volume")

    def __getitem__(self, key):
        if isinstance(key, int) and 0 <= key < len(self._legacy_keys):
            key = self._legacy_keys[key]
        return super().__getitem__(key)


# ============================================================
# HELPERS
# ============================================================

def _num(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _timeframe_ms(value: Any) -> int:
    if isinstance(value, str):
        key = value.strip()
        canonical = TIMEFRAME_ALIASES.get(key.upper(), key.lower())
        if canonical not in TIMEFRAME_MS:
            raise ValueError(f"Unsupported timeframe: {value}")
        return TIMEFRAME_MS[canonical]

    try:
        interval = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeframe_ms must be an integer or timeframe string") from exc

    if interval <= 0:
        raise ValueError("timeframe_ms must be positive")
    return interval


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ============================================================
# NORMALIZATION
# ============================================================

def convert_candles(rows: Iterable[Any] | None) -> List[Candle]:
    out: list[Candle] = []

    for row in rows or []:
        if isinstance(row, dict):
            t = row.get("time", row.get("timestamp", row.get("openTime", row.get("ts"))))
            o = row.get("open", row.get("o"))
            h = row.get("high", row.get("h"))
            l = row.get("low", row.get("l"))
            c = row.get("close", row.get("c"))
            v = row.get("volume", row.get("vol", row.get("q", 0)))
        else:
            try:
                if len(row) < 6:
                    continue
                t, o, h, l, c, v = row[:6]
            except TypeError:
                continue

        try:
            ts = int(float(t))
            if ts < 10**12:
                ts *= 1000

            candle = Candle(
                time=ts,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v or 0),
            )

            if not all(math.isfinite(float(candle[k])) for k in
                       ("open", "high", "low", "close", "volume")):
                continue

            if min(candle["open"], candle["high"], candle["low"], candle["close"]) <= 0:
                continue

            if candle["low"] > candle["high"]:
                continue

            out.append(candle)

        except (TypeError, ValueError, OverflowError):
            continue

    out.sort(key=lambda x: int(x["time"]))

    dedup: dict[int, Candle] = {}
    for candle in out:
        dedup[int(candle["time"])] = candle

    return [dedup[t] for t in sorted(dedup)]


def closed_candle_rows(
    candles: Iterable[Any] | None,
    timeframe_ms: Any,
    now_ms: Optional[int] = None,
) -> List[Candle]:
    interval = _timeframe_ms(timeframe_ms)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    out = convert_candles(candles)
    return [c for c in out if int(c["time"]) + interval <= now]


# ============================================================
# INDICATORS
# ============================================================

def _safe_ema(values: List[float], period: int) -> Optional[float]:
    try:
        return float(ema(values, period))
    except Exception:
        return None


def _ema_series(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []

    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    result = [current]

    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
        result.append(current)

    return result


def _ema_slope(values: List[float], period: int, lookback: int = 5) -> float:
    series = _ema_series(values, period)
    if len(series) <= lookback:
        return 0.0

    base = abs(series[-lookback - 1])
    if base <= 0:
        return 0.0

    return (series[-1] - series[-lookback - 1]) / base


def _safe_rsi(values: List[float], period: int = 14) -> float:
    try:
        return _num(rsi(values, period), 50.0)
    except Exception:
        return 50.0


def _true_ranges(candles: List[Candle]) -> List[float]:
    if not candles:
        return []

    result = [float(candles[0]["high"]) - float(candles[0]["low"])]
    previous = float(candles[0]["close"])

    for candle in candles[1:]:
        high = float(candle["high"])
        low = float(candle["low"])
        result.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = float(candle["close"])

    return result


def _safe_atr(candles: List[Candle], period: int = 14) -> float:
    try:
        return max(0.0, _num(atr(candles, period)))
    except Exception:
        return 0.0


def _atr_series(candles: List[Candle], period: int = 14) -> List[float]:
    trs = _true_ranges(candles)
    if not trs:
        return []

    result = [0.0] * len(trs)
    if len(trs) <= period:
        return result

    window = sum(trs[1:period + 1])
    result[period] = window / period

    for i in range(period + 1, len(trs)):
        window += trs[i]
        window -= trs[i - period]
        result[i] = window / period

    return result


def _atr_percent(price: float, atr_value: float) -> float:
    return atr_value / price if price > 0 else 0.0


def _adx(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < period * 2 + 2:
        return 0.0

    trs, plus_dm, minus_dm = [], [], []

    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        high, low = float(cur["high"]), float(cur["low"])
        prev_high, prev_low = float(prev["high"]), float(prev["low"])
        prev_close = float(prev["close"])

        up = high - prev_high
        down = prev_low - low

        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    if len(trs) < period * 2:
        return 0.0

    tr_s = sum(trs[:period]) / period
    plus_s = sum(plus_dm[:period]) / period
    minus_s = sum(minus_dm[:period]) / period
    dx = []

    for i in range(period, len(trs)):
        tr_s = (tr_s * (period - 1) + trs[i]) / period
        plus_s = (plus_s * (period - 1) + plus_dm[i]) / period
        minus_s = (minus_s * (period - 1) + minus_dm[i]) / period

        pdi = 100.0 * plus_s / tr_s if tr_s else 0.0
        mdi = 100.0 * minus_s / tr_s if tr_s else 0.0
        denominator = pdi + mdi

        dx.append(100.0 * abs(pdi - mdi) / denominator if denominator else 0.0)

    if len(dx) < period:
        return 0.0

    adx = sum(dx[:period]) / period
    for value in dx[period:]:
        adx = (adx * (period - 1) + value) / period

    return adx


def _relative_volume(candles: List[Candle], lookback: int = 20) -> float:
    if len(candles) < lookback + 1:
        return 0.0

    current = float(candles[-1]["volume"])
    previous = candles[-lookback - 1:-1]
    average = sum(float(c["volume"]) for c in previous) / lookback

    return current / average if average > 0 else 0.0


def _atr_percentile(candles: List[Candle], period: int = 14, lookback: int = 100) -> float:
    atr_values = _atr_series(candles, period)

    if len(candles) < period + 10:
        return 50.0

    start = max(period, len(candles) - lookback)
    ratios = []

    for i in range(start, len(candles)):
        price = float(candles[i]["close"])
        a = atr_values[i]
        if price > 0 and a > 0:
            ratios.append(a / price)

    if not ratios:
        return 50.0

    current = ratios[-1]
    return 100.0 * sum(x <= current for x in ratios) / len(ratios)


def _macd(values: List[float]) -> Tuple[float, float, float]:
    fast = _ema_series(values, 12)
    slow = _ema_series(values, 26)

    if not fast or not slow:
        return 0.0, 0.0, 0.0

    n = min(len(fast), len(slow))
    line_series = [fast[-n + i] - slow[-n + i] for i in range(n)]
    signal_series = _ema_series(line_series, 9)

    line = line_series[-1]
    signal = signal_series[-1] if signal_series else 0.0

    return line, signal, line - signal


# ============================================================
# SWINGS / STRUCTURE
# ============================================================

def _swing_highs(candles: List[Candle], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    result = []

    for i in range(left, len(candles) - right):
        high = float(candles[i]["high"])

        if (
            all(high > float(candles[j]["high"]) for j in range(i - left, i))
            and all(high > float(candles[j]["high"]) for j in range(i + 1, i + right + 1))
        ):
            result.append((i, high))

    return result


def _swing_lows(candles: List[Candle], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    result = []

    for i in range(left, len(candles) - right):
        low = float(candles[i]["low"])

        if (
            all(low < float(candles[j]["low"]) for j in range(i - left, i))
            and all(low < float(candles[j]["low"]) for j in range(i + 1, i + right + 1))
        ):
            result.append((i, low))

    return result


def _protected_structure(candles: List[Candle]) -> Dict[str, Any]:
    highs = _swing_highs(candles)
    lows = _swing_lows(candles)

    protected_high = highs[-1][1] if highs else None
    protected_low = lows[-1][1] if lows else None

    if len(highs) < 2 or len(lows) < 2:
        return {
            "state": "NEUTRAL",
            "protected_high": protected_high,
            "protected_low": protected_low,
        }

    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]

    if h2 > h1 and l2 > l1:
        state = "BULLISH"
    elif h2 < h1 and l2 < l1:
        state = "BEARISH"
    else:
        state = "NEUTRAL"

    return {
        "state": state,
        "protected_high": protected_high,
        "protected_low": protected_low,
    }


def _recent_swing_direction(candles: List[Candle], lookback: int = 60) -> Dict[str, Any]:
    sample = candles[-lookback:] if len(candles) > lookback else candles
    highs = _swing_highs(sample)
    lows = _swing_lows(sample)

    result = {
        "bull_higher_high": False,
        "bull_higher_low": False,
        "bear_lower_high": False,
        "bear_lower_low": False,
        "bull_score": 0,
        "bear_score": 0,
    }

    if len(highs) >= 2:
        result["bull_higher_high"] = highs[-1][1] > highs[-2][1]
        result["bear_lower_high"] = highs[-1][1] < highs[-2][1]

    if len(lows) >= 2:
        result["bull_higher_low"] = lows[-1][1] > lows[-2][1]
        result["bear_lower_low"] = lows[-1][1] < lows[-2][1]

    result["bull_score"] = int(result["bull_higher_high"]) + int(result["bull_higher_low"])
    result["bear_score"] = int(result["bear_lower_high"]) + int(result["bear_lower_low"])

    return result


# ============================================================
# 4H REGIME
# ============================================================

def _four_hour_regime(candles: List[Candle]) -> Dict[str, Any]:
    close = [float(c["close"]) for c in candles]

    e21 = _safe_ema(close, 21)
    e50 = _safe_ema(close, 50)
    e100 = _safe_ema(close, 100)
    e200 = _safe_ema(close, 200)

    atr4 = _safe_atr(candles)
    adx4 = _adx(candles)
    slope4 = _ema_slope(close, 50)
    protected = _protected_structure(candles)
    swings = _recent_swing_direction(candles, 80)

    current = close[-1]

    if None in (e21, e50, e100, e200):
        return {
            "bull": False, "bear": False, "regime": "NO_TRADE",
            "e21": e21, "e50": e50, "e100": e100, "e200": e200,
            "atr": atr4, "adx": adx4, "slope": slope4,
            "protected": protected, "swings": swings,
        }

    bull_votes = 0
    bear_votes = 0

    bull_votes += int(current > e200)
    bull_votes += int(e21 >= e50)
    bull_votes += int(slope4 >= -0.0002)
    bull_votes += int(adx4 >= ADX_TREND_MIN)
    bull_votes += int(protected["state"] == "BULLISH")
    bull_votes += int(swings["bull_score"] >= 1)

    bear_votes += int(current < e200)
    bear_votes += int(e21 <= e50)
    bear_votes += int(slope4 <= 0.0002)
    bear_votes += int(adx4 >= ADX_TREND_MIN)
    bear_votes += int(protected["state"] == "BEARISH")
    bear_votes += int(swings["bear_score"] >= 1)

    bull = current > e200 and e21 >= e50 and bull_votes >= 4 and bull_votes > bear_votes
    bear = current < e200 and e21 <= e50 and bear_votes >= 4 and bear_votes > bull_votes

    regime = "BULLISH" if bull else "BEARISH" if bear else "NO_TRADE"

    return {
        "bull": bool(bull),
        "bear": bool(bear),
        "regime": regime,
        "e21": e21,
        "e50": e50,
        "e100": e100,
        "e200": e200,
        "atr": atr4,
        "adx": adx4,
        "slope": slope4,
        "protected": protected,
        "swings": swings,
        "bull_votes": bull_votes,
        "bear_votes": bear_votes,
    }


# ============================================================
# 1H DIRECTIONAL EVIDENCE
# ============================================================

def _one_hour_alignment(candles: List[Candle], regime4: Dict[str, Any]) -> Dict[str, Any]:
    close = [float(c["close"]) for c in candles]
    price = close[-1]

    e21 = _safe_ema(close, 21)
    e50 = _safe_ema(close, 50)
    e200 = _safe_ema(close, 200)

    structure = get_structure(candles)
    protected = _protected_structure(candles)
    swings = _recent_swing_direction(candles, 70)

    slope = _ema_slope(close, 50)
    r = _safe_rsi(close)
    a = _safe_atr(candles)

    if e21 is None or e50 is None:
        return {
            "long": False, "short": False,
            "structure": structure,
            "protected": protected,
            "swings": swings,
            "e21": e21, "e50": e50, "e200": e200,
            "slope": slope, "rsi": r, "atr": a,
            "long_votes": 0, "short_votes": 0,
        }

    tolerance = price * EMA_TOLERANCE_PCT

    # Independent 1H evidence groups.
    long_ema = (
        price >= e50 - tolerance
        and e21 >= e50
        and slope >= -0.0010
    )

    short_ema = (
        price <= e50 + tolerance
        and e21 <= e50
        and slope <= 0.0010
    )

    long_structure = (
        structure == "HH/HL"
        or protected["state"] == "BULLISH"
        or swings["bull_score"] >= 1
    )

    short_structure = (
        structure == "LH/LL"
        or protected["state"] == "BEARISH"
        or swings["bear_score"] >= 1
    )

    long_momentum = r >= 50.0 and (e200 is None or price >= e200 * 0.995)
    short_momentum = r <= 50.0 and (e200 is None or price <= e200 * 1.005)

    long_slope = slope >= -0.0010
    short_slope = slope <= 0.0010

    long_votes = sum((
        bool(long_ema),
        bool(long_structure),
        bool(long_momentum),
        bool(long_slope),
    ))

    short_votes = sum((
        bool(short_ema),
        bool(short_structure),
        bool(short_momentum),
        bool(short_slope),
    ))

    # Regime remains a hard directional constraint.
    long = bool(
        regime4.get("bull")
        and long_votes >= 3
        and long_votes > short_votes
    )

    short = bool(
        regime4.get("bear")
        and short_votes >= 3
        and short_votes > long_votes
    )

    return {
        "long": long,
        "short": short,
        "structure": structure,
        "protected": protected,
        "swings": swings,
        "e21": e21,
        "e50": e50,
        "e200": e200,
        "slope": slope,
        "rsi": r,
        "atr": a,
        "long_votes": long_votes,
        "short_votes": short_votes,
        "long_ema": long_ema,
        "short_ema": short_ema,
        "long_structure": long_structure,
        "short_structure": short_structure,
        "long_momentum": long_momentum,
        "short_momentum": short_momentum,
    }


# ============================================================
# BOS / RETEST
# ============================================================

def _bos_strength(candle: Candle, level: float, atr_value: float) -> float:
    rng = max(float(candle["high"]) - float(candle["low"]), 1e-12)

    body_ratio = abs(float(candle["close"]) - float(candle["open"])) / rng
    displacement = abs(float(candle["close"]) - level) / atr_value if atr_value > 0 else 0.0

    return _clamp(
        0.50 * _clamp(body_ratio / 0.55, 0.0, 1.0)
        + 0.50 * _clamp(displacement / 0.50, 0.0, 1.0),
        0.0,
        1.0,
    )


def _bos_events(candles: List[Candle], side: str, lookback: int = 70) -> List[Dict[str, Any]]:
    if len(candles) < 10 or side not in {"LONG", "SHORT"}:
        return []

    atr_values = _atr_series(candles, 14)
    pivots = _swing_highs(candles) if side == "LONG" else _swing_lows(candles)
    events = []
    start = max(1, len(candles) - lookback)

    for i in range(start, len(candles)):
        a = atr_values[i]
        if a <= 0:
            continue

        close = float(candles[i]["close"])
        prev_close = float(candles[i - 1]["close"])

        buffer = max(a * BOS_BUFFER_ATR, close * BOS_BUFFER_PCT)

        candidates = [(idx, price) for idx, price in pivots if idx + 2 <= i]

        for swing_index, level in reversed(candidates):
            level = float(level)

            if side == "LONG":
                crossed = prev_close <= level + buffer and close > level + buffer
            else:
           
