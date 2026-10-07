from __future__ import annotations

"""Deterministic MEXC Futures signal engine.

Authoritative analysis timeframes are exactly:
    1D -> 12H -> 4H -> 1H

MEXC Futures does not expose a native 12H kline interval, so 12H candles are
causally synthesized from three completed 4H candles.
"""

import math
import os
import time
from bisect import bisect_right
from typing import Any, Iterable, List, Optional, Tuple

from .indicators import atr, ema, rsi, volume_status

TIMEFRAME_MS = {
    "1h": 3_600_000,
    "4h": 14_400_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
TIMEFRAME_ALIASES = {
    "1H": "1h", "1HR": "1h", "1HOUR": "1h",
    "4H": "4h", "4HR": "4h", "4HOUR": "4h",
    "12H": "12h", "12HR": "12h", "12HOUR": "12h",
    "1D": "1d", "1DAY": "1d",
}
APPROVED_TIMEFRAMES = ("1D", "12H", "4H", "1H")

MIN_SCORE = 0
MIN_RR = 1.60
MIN_SL_ATR = 0.50
MAX_SL_ATR = 1.25
MIN_TP_ATR = 0.50
MIN_ATR_PERCENTILE = 5.0
MAX_ATR_PERCENTILE = 98.0
BOS_BUFFER_ATR = 0.10
RETEST_TOLERANCE_ATR = 0.25
RETEST_PENETRATION_ATR = 0.75
RETEST_INVALIDATION_ATR = 0.25
MAX_SETUP_AGE_4H = 18  # 72h
MAX_RETEST_BARS_4H = 18
MAX_ENTRY_DISTANCE_ATR = 0.35  # signed extension measured in 1H ATR
MAX_LIMIT_ENTRY_DISTANCE_ATR = 0.85
MIN_DEPARTURE_ATR = 0.60
MAX_DEPARTURE_BARS_4H = 6
MAX_RETEST_1H_BARS = 12
RETEST_ZONE_FLOOR_ATR = 0.35
RETEST_ZONE_CEILING_ATR = 0.35
RETEST_WICK_MIN = 0.30
LIMIT_ENTRY_EXPIRY_BARS = 3
BTC_SHOCK_ATR = 2.0
ADX_TREND_MIN = 14.0
MIN_TRIGGER_BODY = 0.25
MIN_TRIGGER_CLOSE_LOCATION = 0.58
MIN_TRIGGER_RVOL = 0.70
MAX_TRIGGER_BARS_1H = 2
DEFAULT_MAX_HOLD_MINUTES = 72 * 60
ENGINE_VERSION = "V11-balanced-value-pullback-liquidity-reclaim"

CONFIRMATION_FAMILY_NAMES = (
    "momentum",
    "relative_volume",
    "volatility_regime",
    "target_path",
    "vwap_location",
    "structure_quality",
    "entry_quality",
)


class Candle(dict):
    _legacy_keys = ("time", "open", "high", "low", "close", "volume")

    def __getitem__(self, key):
        if isinstance(key, int) and 0 <= key < len(self._legacy_keys):
            key = self._legacy_keys[key]
        return super().__getitem__(key)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _timeframe_ms(value: Any) -> int:
    if isinstance(value, str):
        canonical = TIMEFRAME_ALIASES.get(value.strip().upper(), value.strip().lower())
        if canonical not in TIMEFRAME_MS:
            raise ValueError(f"Unsupported analysis timeframe: {value}. Allowed: {', '.join(APPROVED_TIMEFRAMES)}")
        return TIMEFRAME_MS[canonical]
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeframe must be a supported timeframe string") from exc
    if number not in set(TIMEFRAME_MS.values()):
        raise ValueError("Numeric timeframe values are limited to the approved analysis timeframes")
    return number


def convert_candles(rows: Iterable[Any] | None) -> List[Candle]:
    output: list[Candle] = []
    for row in rows or []:
        try:
            if isinstance(row, dict):
                t = row.get("time", row.get("timestamp", row.get("openTime", row.get("ts"))))
                o = row.get("open", row.get("o"))
                h = row.get("high", row.get("h"))
                l = row.get("low", row.get("l"))
                c = row.get("close", row.get("c"))
                v = row.get("volume", row.get("vol", row.get("q", 0)))
            else:
                if len(row) < 6:
                    continue
                t, o, h, l, c, v = row[:6]
            ts = int(float(t))
            if ts < 10**12:
                ts *= 1000
            candle = Candle(time=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v or 0.0))
            vals = [candle[k] for k in Candle._legacy_keys]
            if not all(math.isfinite(float(x)) for x in vals):
                continue
            if min(candle["open"], candle["high"], candle["low"], candle["close"]) <= 0:
                continue
            if candle["volume"] < 0:
                continue
            if candle["low"] > candle["high"] or not (candle["low"] <= candle["close"] <= candle["high"]):
                continue
            output.append(candle)
        except (TypeError, ValueError, OverflowError, KeyError):
            continue
    dedup: dict[int, Candle] = {}
    for candle in sorted(output, key=lambda x: int(x["time"])):
        dedup[int(candle["time"])] = candle
    return [dedup[k] for k in sorted(dedup)]


def _coerce_candles(rows: Iterable[Any] | None) -> List[Candle]:
    """Return normalized Candle objects without reprocessing an already-normalized list."""
    if isinstance(rows, list) and (not rows or isinstance(rows[0], Candle)):
        return rows
    return convert_candles(rows)


def closed_candle_rows(candles: Iterable[Any] | None, timeframe_ms: Any, now_ms: Optional[int] = None) -> List[Candle]:
    interval = _timeframe_ms(timeframe_ms)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    source = _coerce_candles(candles)
    return [c for c in source if int(c["time"]) + interval <= now]


def synthesize_12h_from_4h(candles_4h: Iterable[Any] | None, *, now_ms: Optional[int] = None) -> list[Candle]:
    """Aggregate three completed 4H bars into a UTC-aligned 12H bar.

    A group is accepted only when its three 4H opens are exactly contiguous.
    This prevents missing/partial source data from becoming synthetic 12H data.
    """
    c4 = closed_candle_rows(candles_4h or [], "4H", now_ms)
    groups: dict[int, list[Candle]] = {}
    for candle in c4:
        bucket = (int(candle["time"]) // TIMEFRAME_MS["12h"]) * TIMEFRAME_MS["12h"]
        groups.setdefault(bucket, []).append(candle)
    output: list[Candle] = []
    for bucket, group in sorted(groups.items()):
        group = sorted(group, key=lambda x: int(x["time"]))
        if len(group) != 3:
            continue
        expected = [bucket + i * TIMEFRAME_MS["4h"] for i in range(3)]
        if [int(x["time"]) for x in group] != expected:
            continue
        output.append(Candle(
            time=bucket,
            open=float(group[0]["open"]),
            high=max(float(x["high"]) for x in group),
            low=min(float(x["low"]) for x in group),
            close=float(group[-1]["close"]),
            volume=sum(float(x["volume"]) for x in group),
        ))
    return output


def _safe_ema(values: list[float], period: int) -> float | None:
    try:
        value = ema(values, period)
        return float(value) if math.isfinite(float(value)) else None
    except Exception:
        return None


def _ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    result = [current]
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
        result.append(current)
    return result


def _ema_slope(values: list[float], period: int, lookback: int = 5) -> float:
    series = _ema_series(values, period)
    if len(series) <= lookback:
        return 0.0
    base = abs(series[-lookback - 1])
    return (series[-1] - series[-lookback - 1]) / base if base > 0 else 0.0


def _safe_rsi(values: list[float], period: int = 14) -> float:
    try:
        return _num(rsi(values, period), 50.0)
    except Exception:
        return 50.0


def _true_ranges(candles: list[Candle]) -> list[float]:
    if not candles:
        return []
    result = [float(candles[0]["high"]) - float(candles[0]["low"])]
    prev = float(candles[0]["close"])
    for c in candles[1:]:
        h, l = float(c["high"]), float(c["low"])
        result.append(max(h - l, abs(h - prev), abs(l - prev)))
        prev = float(c["close"])
    return result


def _safe_atr(candles: list[Candle], period: int = 14) -> float:
    try:
        return max(0.0, _num(atr(candles, period)))
    except Exception:
        return 0.0


def _atr_series(candles: list[Candle], period: int = 14) -> list[float]:
    trs = _true_ranges(candles)
    result = [0.0] * len(trs)
    if len(trs) <= period:
        return result
    window = sum(trs[1:period + 1])
    result[period] = window / period
    for i in range(period + 1, len(trs)):
        window += trs[i] - trs[i - period]
        result[i] = window / period
    return result


def _atr_percentile(candles: list[Candle], period: int = 14, lookback: int = 100) -> float:
    values = _atr_series(candles, period)
    ratios: list[float] = []
    for i in range(max(period, len(candles) - lookback), len(candles)):
        price = _num(candles[i]["close"])
        a = _num(values[i] if i < len(values) else 0.0)
        if price > 0 and a > 0:
            ratios.append(a / price)
    if not ratios:
        return 50.0
    current = ratios[-1]
    return 100.0 * sum(x <= current for x in ratios) / len(ratios)


def _adx(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period * 2 + 2:
        return 0.0
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        up = float(cur["high"]) - float(prev["high"])
        down = float(prev["low"]) - float(cur["low"])
        trs.append(max(float(cur["high"]) - float(cur["low"]), abs(float(cur["high"]) - float(prev["close"])), abs(float(cur["low"]) - float(prev["close"]))))
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    if len(trs) < period * 2:
        return 0.0
    tr_s = sum(trs[:period]) / period
    plus_s = sum(plus_dm[:period]) / period
    minus_s = sum(minus_dm[:period]) / period
    dx: list[float] = []
    for i in range(period, len(trs)):
        tr_s = (tr_s * (period - 1) + trs[i]) / period
        plus_s = (plus_s * (period - 1) + plus_dm[i]) / period
        minus_s = (minus_s * (period - 1) + minus_dm[i]) / period
        pdi = 100.0 * plus_s / tr_s if tr_s else 0.0
        mdi = 100.0 * minus_s / tr_s if tr_s else 0.0
        den = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / den if den else 0.0)
    if len(dx) < period:
        return 0.0
    value = sum(dx[:period]) / period
    for x in dx[period:]:
        value = (value * (period - 1) + x) / period
    return value


def _relative_volume(candles: list[Candle], lookback: int = 20) -> float:
    if len(candles) < lookback + 1:
        return 0.0
    average = sum(float(c["volume"]) for c in candles[-lookback - 1:-1]) / lookback
    return float(candles[-1]["volume"]) / average if average > 0 else 0.0


def _macd_components(values: list[float]) -> tuple[float, float, float, float]:
    fast = _ema_series(values, 12)
    slow = _ema_series(values, 26)
    if not fast or not slow:
        return 0.0, 0.0, 0.0, 0.0
    n = min(len(fast), len(slow))
    macd_series = [fast[-n + i] - slow[-n + i] for i in range(n)]
    signal_series = _ema_series(macd_series, 9)
    line = macd_series[-1]
    signal = signal_series[-1] if signal_series else 0.0
    hist = line - signal
    if len(macd_series) < 2 or len(signal_series) < 2:
        return line, signal, hist, 0.0
    prev_hist = macd_series[-2] - signal_series[-2]
    scale = max(abs(hist), abs(prev_hist), 1e-12)
    delta = (hist - prev_hist) / scale
    return line, signal, hist, delta

def _swing_points(candles: list[Candle], left: int = 2, right: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(left, len(candles) - right):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        if all(h > float(candles[j]["high"]) for j in range(i - left, i)) and all(h > float(candles[j]["high"]) for j in range(i + 1, i + right + 1)):
            highs.append((i, h))
        if all(l < float(candles[j]["low"]) for j in range(i - left, i)) and all(l < float(candles[j]["low"]) for j in range(i + 1, i + right + 1)):
            lows.append((i, l))
    return highs, lows


def _structure_from_swings(highs: list[tuple[int, float]], lows: list[tuple[int, float]]) -> str:
    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"
    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]
    if h2 > h1 and l2 > l1:
        return "HH/HL"
    if h2 < h1 and l2 < l1:
        return "LH/LL"
    return "RANGE"
































def _data_quality(candles: list[Candle], timeframe: str, minimum: int) -> tuple[bool, str]:
    if len(candles) < minimum:
        return False, f"{timeframe}: not enough closed candles ({len(candles)}<{minimum})"
    interval = TIMEFRAME_MS[timeframe.lower()]
    times = [int(c["time"]) for c in candles[-minimum:]]
    if any(b <= a for a, b in zip(times, times[1:])):
        return False, f"{timeframe}: non-monotonic timestamps"
    if any(b - a > interval for a, b in zip(times, times[1:])):
        return False, f"{timeframe}: candle gap inside analysis window"
    return True, "OK"








# V11 strategy constants. Score is diagnostic only; it is never an acceptance gate.
def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value < minimum or value > maximum:
        return float(default)
    return float(value)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return int(default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return value if minimum <= value <= maximum else int(default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


V11_MIN_RR = 1.60
# Controlled threshold experiment. Default preserves the frozen V11 control.
V11_MIN_IMPULSE_ATR = _env_float(
    "V11_MIN_IMPULSE_ATR", 2.50, minimum=0.50, maximum=8.00
)
# Controlled A/B experiment switch. Default preserves the frozen V11 control.
# SWEEP = current V11 stop; 4H_ORIGIN = experimental HTF structural stop.
V11_SL_MODE = str(os.getenv("V11_SL_MODE", "SWEEP")).strip().upper() or "SWEEP"
if V11_SL_MODE not in {"SWEEP", "4H_ORIGIN"}:
    V11_SL_MODE = "SWEEP"
# Controlled sweep -> current-reclaim search-window experiment. The final
# reclaim must still be on the current 1H decision candle; this parameter only
# controls how far back the causal sweep may be relative to that reclaim.
V11_MAX_TRIGGER_BARS = _env_int(
    "V11_MAX_TRIGGER_BARS", 6, minimum=1, maximum=24
)
# Controlled SHORT experiment. When enabled, only 1D RANGE structure may
# bypass the normal daily short permission, and the normal 12H/4H/1H gates
# remain mandatory. Default is OFF.
V11_SHORT_RANGE_RELAXED = _env_bool("V11_SHORT_RANGE_RELAXED", False)

V11_SETUP_MAX_4H_BARS = 30
V11_STOP_BUFFER_ATR_4H = 0.20
V11_STOP_BUFFER_ATR_1H = 0.15
V11_MIN_SL_ATR_SANITY = 0.20
V11_MAX_SL_ATR_SANITY = 3.00
V11_SHOCK_RANGE_ATR = 4.50

def _v11_structure(candles: list[Candle], left: int = 3, right: int = 3) -> str:
    highs, lows = _swing_points(candles, left=left, right=right)
    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"
    if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]:
        return "HH/HL"
    if highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
        return "LH/LL"
    return "RANGE"


def _v11_regime_1d(candles: list[Candle]) -> dict[str, Any]:
    """Build a directional 1D permission from independent macro votes.

    The original V11 implementation required every bullish/bearish component
    to agree (EMA placement + price location + exact HH/HL or LH/LL). That made
    the macro layer behave like a hard all-or-nothing structure gate and produced
    large neutral regions even when the broader trend evidence was aligned.

    V11.2 keeps the 1D layer directional and causal, but uses the proven V10-style
    3-of-5 macro vote model: price vs EMA200, EMA50 vs EMA200, confirmed structure,
    EMA50 slope, and ADX. Structure remains visible in diagnostics but is no longer
    required to be the sole deciding vote. The 4H setup still supplies the strict
    trend-continuation structure requirement.
    """
    closes = [float(c["close"]) for c in candles]
    e21 = _safe_ema(closes, 21)
    e50 = _safe_ema(closes, 50)
    e200 = _safe_ema(closes, 200)
    slope = _ema_slope(closes, 50, lookback=5)
    adx = _adx(candles)
    structure = _v11_structure(candles, 3, 3)
    price = closes[-1]

    if e50 is None or e200 is None:
        return {
            "regime": "NEUTRAL", "bull": False, "bear": False,
            "e21": e21, "e50": e50, "e200": e200, "slope": slope,
            "adx": adx, "structure": structure, "price": price,
            "bull_votes": 0, "bear_votes": 0,
        }

    bull_votes = sum((
        price > e200,
        e50 > e200,
        structure == "HH/HL",
        slope > 0,
        adx >= ADX_TREND_MIN,
    ))
    bear_votes = sum((
        price < e200,
        e50 < e200,
        structure == "LH/LL",
        slope < 0,
        adx >= ADX_TREND_MIN,
    ))
    bull = bull_votes >= 3 and bull_votes > bear_votes
    bear = bear_votes >= 3 and bear_votes > bull_votes
    return {
        "regime": "BULLISH" if bull else "BEARISH" if bear else "NEUTRAL",
        "bull": bull,
        "bear": bear,
        "e21": e21,
        "e50": e50,
        "e200": e200,
        "slope": slope,
        "adx": adx,
        "structure": structure,
        "price": price,
        "bull_votes": bull_votes,
        "bear_votes": bear_votes,
    }


def _v11_context_12h(candles: list[Candle], side: str) -> dict[str, Any]:
    closes = [float(c["close"]) for c in candles]
    e21 = _safe_ema(closes, 21)
    e50 = _safe_ema(closes, 50)
    slope = _ema_slope(closes, 50, lookback=5)
    structure = _v11_structure(candles, 3, 3)
    if e21 is None or e50 is None:
        return {"status": "NEUTRAL", "hostile": False, "structure": structure, "e21": e21, "e50": e50, "slope": slope}
    price = closes[-1]
    if side == "LONG":
        hostile = bool(structure == "LH/LL" and price < e50 and slope < 0)
        healthy = bool(not hostile and (structure == "HH/HL" or price >= e50))
    else:
        hostile = bool(structure == "HH/HL" and price > e50 and slope > 0)
        healthy = bool(not hostile and (structure == "LH/LL" or price <= e50))
    return {
        "status": "HOSTILE" if hostile else "HEALTHY" if healthy else "NEUTRAL",
        "hostile": hostile,
        "healthy": healthy,
        "structure": structure,
        "e21": e21,
        "e50": e50,
        "slope": slope,
        "price": price,
    }


def _v11_find_impulses(candles: list[Candle], side: str, max_age: int = 30) -> list[dict[str, Any]]:
    """Find completed 4H impulse legs without future leakage.

    Pivots are 3/3, so a pivot is only used after its three right-side candles
    are closed. Multiple active candidates are returned; the caller may select
    the newest still-valid setup rather than forcing a single latest-only setup.
    """
    if side not in {"LONG", "SHORT"} or len(candles) < 30:
        return []
    highs, lows = _swing_points(candles, left=3, right=3)
    atrs = _atr_series(candles, 14)
    events: list[dict[str, Any]] = []
    if side == "LONG":
        for hi_pos, (hi_idx, hi_price) in enumerate(highs):
            if hi_idx < 7 or len(candles) - 1 - hi_idx > max_age:
                continue
            prior_high = highs[hi_pos - 1][1] if hi_pos > 0 else None
            if prior_high is None or hi_price <= prior_high:
                continue
            eligible_lows = [(idx, price) for idx, price in lows if idx < hi_idx]
            if len(eligible_lows) < 2:
                continue
            lo_idx, lo_price = eligible_lows[-1]
            prev_lo_idx, prev_lo_price = eligible_lows[-2]
            if lo_idx < 3 or lo_price <= prev_lo_price or prev_lo_idx >= lo_idx:
                continue
            leg = hi_price - lo_price
            atr4 = _num(atrs[hi_idx] if hi_idx < len(atrs) else 0.0)
            if atr4 <= 0 or leg < V11_MIN_IMPULSE_ATR * atr4:
                continue
            events.append({
                "side": side, "low_idx": lo_idx, "high_idx": hi_idx,
                "low": float(lo_price), "high": float(hi_price),
                "atr": atr4, "leg": leg, "leg_atr": leg / atr4,
                "structure_label": "HH/HL",
                "high_time": int(candles[hi_idx]["time"]),
                "low_time": int(candles[lo_idx]["time"]),
            })
    else:
        for lo_pos, (lo_idx, lo_price) in enumerate(lows):
            if lo_idx < 7 or len(candles) - 1 - lo_idx > max_age:
                continue
            prior_low = lows[lo_pos - 1][1] if lo_pos > 0 else None
            if prior_low is None or lo_price >= prior_low:
                continue
            eligible_highs = [(idx, price) for idx, price in highs if idx < lo_idx]
            if len(eligible_highs) < 2:
                continue
            hi_idx, hi_price = eligible_highs[-1]
            prev_hi_idx, prev_hi_price = eligible_highs[-2]
            if hi_idx < 3 or hi_price >= prev_hi_price or prev_hi_idx >= hi_idx:
                continue
            leg = hi_price - lo_price
            atr4 = _num(atrs[lo_idx] if lo_idx < len(atrs) else 0.0)
            if atr4 <= 0 or leg < V11_MIN_IMPULSE_ATR * atr4:
                continue
            events.append({
                "side": side, "low_idx": lo_idx, "high_idx": hi_idx,
                "low": float(lo_price), "high": float(hi_price),
                "atr": atr4, "leg": leg, "leg_atr": leg / atr4,
                "structure_label": "LH/LL",
                "high_time": int(candles[hi_idx]["time"]),
                "low_time": int(candles[lo_idx]["time"]),
            })
    return sorted(events, key=lambda x: int(x["high_idx"] if side == "LONG" else x["low_idx"]))


def _v11_value_zone(leg: dict[str, Any], ema21: float | None, ema50: float | None) -> dict[str, float | bool]:
    low = float(leg["low"])
    high = float(leg["high"])
    rng = high - low
    if rng <= 0:
        return {"ok": False}
    side = str(leg["side"])
    if side == "LONG":
        retrace_low = high - 0.786 * rng
        retrace_high = high - 0.382 * rng
    else:
        retrace_low = low + 0.382 * rng
        retrace_high = low + 0.786 * rng
    if ema21 is None or ema50 is None:
        return {"ok": True, "low": retrace_low, "high": retrace_high, "deep_low": retrace_low, "deep_high": retrace_high, "retracement_mid": (retrace_low + retrace_high) / 2.0}
    corridor_low = min(ema21, ema50) - 0.15 * float(leg["atr"])
    corridor_high = max(ema21, ema50) + 0.15 * float(leg["atr"])
    zone_low = max(retrace_low, corridor_low)
    zone_high = min(retrace_high, corridor_high)
    if zone_low >= zone_high:
        zone_low, zone_high = retrace_low, retrace_high
    if side == "LONG":
        deep_low = high - 0.786 * rng
        deep_high = high - 0.50 * rng
    else:
        deep_low = low + 0.50 * rng
        deep_high = low + 0.786 * rng
    return {
        "ok": zone_low < zone_high,
        "low": zone_low,
        "high": zone_high,
        "deep_low": min(deep_low, deep_high),
        "deep_high": max(deep_low, deep_high),
        "retracement_mid": (retrace_low + retrace_high) / 2.0,
        "retracement_low": retrace_low,
        "retracement_high": retrace_high,
    }


def _v11_value_touched(candles_1h: list[Candle], start_time: int, zone: dict[str, Any], side: str) -> tuple[bool, int | None, float | None, float | None]:
    zlow, zhigh = _num(zone.get("low")), _num(zone.get("high"))
    if zlow <= 0 or zhigh <= 0:
        return False, None, None, None
    for i, c in enumerate(candles_1h):
        if int(c["time"]) <= start_time:
            continue
        h, l = float(c["high"]), float(c["low"])
        if l <= zhigh and h >= zlow:
            return True, i, l, h
    return False, None, None, None


def _v11_liquidity_trigger(candles: list[Candle], start_idx: int, side: str, max_bars: int = V11_MAX_TRIGGER_BARS) -> dict[str, Any]:
    """Causal sweep + reclaim trigger.

    The swept level is derived only from the three candles immediately before
    the sweep candle. The reclaim candle must close back through that level.
    No centered pivot is used for the trigger.
    """
    empty = {
        "ready": False, "sweep_idx": None, "reclaim_idx": None, "swept_level": None,
        "rvol": 0.0, "body_ratio": 0.0, "close_location": 0.0,
        "quality": 0.0, "reason": "no causal liquidity sweep + reclaim",
    }
    if start_idx < 3 or start_idx >= len(candles):
        return empty
    end = min(len(candles) - 1, start_idx + max_bars)
    for i in range(start_idx, end + 1):
        prior = candles[max(0, i - 3):i]
        if len(prior) < 3:
            continue
        c = candles[i]
        if side == "LONG":
            level = min(float(x["low"]) for x in prior)
            # A liquidity sweep is a wick through the prior liquidity pool.
            # The documented V11 sequence is sweep -> subsequent reclaim, so
            # the sweep candle must breach the level but does not need to close
            # beyond it. Requiring a close below/above the level turns many
            # ordinary stop-runs into breakouts and suppresses valid setups.
            swept = float(c["low"]) < level
        else:
            level = max(float(x["high"]) for x in prior)
            swept = float(c["high"]) > level
        if not swept:
            continue
        # Reclaim may occur on any subsequent candle within the full V11.2
        # trigger window. The previous implementation unintentionally limited
        # this to only the next 1–2 candles despite max_bars=6.
        for j in range(i + 1, end + 1):
            r = candles[j]
            ropen, rhigh, rlow, rclose = map(float, (r["open"], r["high"], r["low"], r["close"]))
            rrng = max(rhigh - rlow, 1e-12)
            body = abs(rclose - ropen) / rrng
            loc = (rclose - rlow) / rrng
            if side == "LONG":
                reclaimed = rclose > level and rclose > ropen
                close_loc = loc
            else:
                reclaimed = rclose < level and rclose < ropen
                close_loc = 1.0 - loc
            if not reclaimed:
                continue
            rvol = _relative_volume(candles[:j + 1], 20)
            quality = _clamp(0.45 + 0.20 * _clamp(body / 0.50, 0, 1) + 0.20 * _clamp(rvol / 1.20, 0, 1) + 0.15 * _clamp(close_loc / 0.70, 0, 1), 0, 1)
            return {
                "ready": True, "sweep_idx": i, "reclaim_idx": j, "swept_level": level,
                "rvol": rvol, "body_ratio": body, "close_location": close_loc,
                "quality": quality, "reason": "liquidity sweep absorbed and reclaimed",
                "trigger_time": int(r["time"]), "sweep_time": int(c["time"]),
            }
    return empty


def _v11_target(c4: list[Candle], c12: list[Candle], c1d: list[Candle], entry: float, side: str, impulse: dict[str, Any]) -> dict[str, Any]:
    """Select the nearest meaningful structural target before applying RR.

    Target selection is structural-first. If the nearest valid magnet does not
    clear the post-cost RR floor, the setup is rejected rather than choosing a
    farther level solely to manufacture RR.
    """
    levels: list[tuple[float, str, int]] = []
    for candles, tf, left, right in ((c4, "4H", 3, 3), (c12, "12H", 3, 3), (c1d, "1D", 3, 3)):
        highs, lows = _swing_points(candles, left, right)
        if side == "LONG":
            for idx, price in highs:
                if price > entry:
                    levels.append((float(price), tf, int(candles[idx]["time"])))
        else:
            for idx, price in lows:
                if price < entry:
                    levels.append((float(price), tf, int(candles[idx]["time"])))
    # The impulse extreme is always a legitimate first structural magnet.
    impulse_target = float(impulse["high"] if side == "LONG" else impulse["low"])
    if (side == "LONG" and impulse_target > entry) or (side == "SHORT" and impulse_target < entry):
        levels.append((impulse_target, "4H_IMPULSE", int(impulse["high_time"] if side == "LONG" else impulse["low_time"])))
    if not levels:
        return {"ok": False, "reason": "no structural target beyond entry"}
    if side == "LONG":
        price, tf, ts = min(levels, key=lambda x: x[0] - entry)
    else:
        # For SHORTs, the closest lower structural level is the smallest
        # positive distance below entry. Using max(distance) would select the
        # farthest downside target and could manufacture a blocked path.
        price, tf, ts = min(levels, key=lambda x: entry - x[0])
    return {"ok": True, "price": price, "timeframe": tf, "time": ts, "reason": "nearest structural target"}


def _v11_target_path(
    c4: list[Candle],
    c12: list[Candle],
    c1d: list[Candle],
    entry: float,
    target: float,
    side: str,
    target_time: int | None = None,
) -> dict[str, Any]:
    """Validate that no nearer confirmed HTF obstacle blocks the target.

    This is intentionally limited to completed 4H/12H/1D structure. It does
    not inspect any future candle after the 1H decision close. The target is
    structural-first; this function verifies the path diagnostic independently
    instead of equating “a target exists” with “the path is clear”.
    """
    side = str(side).upper()
    if side not in {"LONG", "SHORT"} or entry <= 0 or target <= 0:
        return {"clear": False, "obstacles": [], "reason": "invalid target-path inputs"}

    obstacles: list[dict[str, Any]] = []
    frames = ((c4, "4H", 3, 3), (c12, "12H", 3, 3), (c1d, "1D", 3, 3))
    for candles, tf, left, right in frames:
        highs, lows = _swing_points(candles, left, right)
        points = highs if side == "LONG" else lows
        for idx, price in points:
            price = float(price)
            ts = int(candles[idx]["time"])
            if target_time is not None and ts == int(target_time):
                continue
            between = entry < price < target if side == "LONG" else target < price < entry
            if between:
                obstacles.append({"price": price, "timeframe": tf, "time": ts})

    # Collapse duplicate price levels across timeframes while retaining all source evidence.
    obstacles.sort(key=lambda x: x["price"], reverse=(side == "SHORT"))
    deduped: list[dict[str, Any]] = []
    for obstacle in obstacles:
        if not deduped or abs(float(obstacle["price"]) - float(deduped[-1]["price"])) > max(abs(entry) * 1e-6, 1e-12):
            deduped.append(obstacle)
    if deduped:
        return {
            "clear": False,
            "obstacles": deduped,
            "reason": f"{len(deduped)} confirmed HTF obstacle(s) between entry and target",
        }
    return {"clear": True, "obstacles": [], "reason": "no confirmed HTF structural obstacle before target"}


def _v11_score(features: dict[str, float]) -> tuple[int, dict[str, int]]:
    """Diagnostic score only. It is deliberately not an acceptance threshold."""
    groups = {
        "impulse_structure": int(round(25 * _clamp(features.get("impulse", 0), 0, 1))),
        "value_location": int(round(25 * _clamp(features.get("value", 0), 0, 1))),
        "liquidity_reclaim": int(round(20 * _clamp(features.get("trigger", 0), 0, 1))),
        "target_geometry": int(round(15 * _clamp(features.get("target", 0), 0, 1))),
        "trend_context": int(round(10 * _clamp(features.get("context", 0), 0, 1))),
        "volatility": int(round(5 * _clamp(features.get("volatility", 0), 0, 1))),
    }
    return sum(groups.values()), groups


def _v11_analyze_side(
    symbol: str,
    side: str,
    c1d: list[Candle],
    c12: list[Candle],
    c4: list[Candle],
    c1: list[Candle],
    daily: dict[str, Any],
    cost_pct: float,
    btc_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate one V11 side and retain the first failing gate for diagnostics."""
    context = _v11_context_12h(c12, side)
    short_range_exception = bool(
        side == "SHORT"
        and V11_SHORT_RANGE_RELAXED
        and str(daily.get("structure") or "").upper() == "RANGE"
        and str(context.get("structure") or "").upper() == "LH/LL"
        and not bool(context.get("hostile"))
    )
    direction_ok = (
        bool(daily.get("bull"))
        if side == "LONG"
        else bool(daily.get("bear")) or short_range_exception
    )

    def reject(reason: str, diagnostic_key: str | None = None) -> dict[str, Any]:
        return {
            "side": side,
            "direction_ok": direction_ok,
            "context": context,
            "candidate": False,
            "reason": str(reason),
            "technical_gate_failures": [f"{side}: {reason}"],
            "rejection_stage": "SETUP",
            "primary_rejection_reason": f"{side}: {reason}",
            "diagnostic_key": diagnostic_key,
        }

    daily_key = f"1D:{int(c1d[-1]['time'])}"
    if not direction_ok:
        return reject(
            f"{side}: 1D trend permission unavailable (regime={daily.get('regime') or 'NEUTRAL'}, structure={daily.get('structure') or 'UNKNOWN'}, bull_votes={daily.get('bull_votes', 0)}, bear_votes={daily.get('bear_votes', 0)})",
            diagnostic_key=daily_key,
        )
    if context.get("hostile"):
        return reject(
            f"{side}: hostile 12H context (structure={context.get('structure') or 'UNKNOWN'})",
            diagnostic_key=f"12H:{int(c12[-1]['time'])}",
        )

    e21_4 = _safe_ema([float(c["close"]) for c in c4], 21)
    e50_4 = _safe_ema([float(c["close"]) for c in c4], 50)
    impulses = _v11_find_impulses(c4, side)
    if not impulses:
        return reject(
            f"{side}: no completed 4H HH/HL or LH/LL impulse >= {V11_MIN_IMPULSE_ATR:.2f} ATR",
            diagnostic_key=f"4H:{int(c4[-1]['time'])}",
        )

    last_reason = f"{side}: no active value-pullback trigger"
    for impulse in reversed(impulses):
        impulse_end_idx = int(impulse["high_idx"]) if side == "LONG" else int(impulse["low_idx"])
        impulse_diag_key = f"IMPULSE:{int(impulse['low_time'])}:{int(impulse['high_time'])}"
        setup_age = len(c4) - 1 - impulse_end_idx
        if setup_age > V11_SETUP_MAX_4H_BARS:
            last_reason = f"{side}: 4H impulse expired ({setup_age} bars > {V11_SETUP_MAX_4H_BARS})"
            continue

        if side == "LONG":
            if any(float(c["close"]) < float(impulse["low"]) for c in c4[impulse_end_idx + 1:]):
                last_reason = f"{side}: 4H impulse invalidated below origin {float(impulse['low']):.8g}"
                continue
        else:
            if any(float(c["close"]) > float(impulse["high"]) for c in c4[impulse_end_idx + 1:]):
                last_reason = f"{side}: 4H impulse invalidated above origin {float(impulse['high']):.8g}"
                continue

        zone = _v11_value_zone(impulse, e21_4, e50_4)
        if not zone.get("ok"):
            last_reason = f"{side}: value zone invalid/non-overlapping"
            continue

        start_time = int(c4[impulse_end_idx]["time"])
        post_impulse = [c for c in c1 if int(c["time"]) > start_time]
        if not post_impulse:
            last_reason = f"{side}: no 1H candles after impulse endpoint"
            continue

        # Structural invalidation is defined on the 4H setup timeframe. A 1H
        # wick may probe the origin without erasing a completed 4H structure,
        # so do not reject the setup merely because an intrabar 1H low/high
        # touches the origin. The completed 4H close test above is authoritative.

        zlow, zhigh = _num(zone.get("low")), _num(zone.get("high"))
        trigger = None
        touched_value = False
        for touch_idx, touch_candle in enumerate(c1):
            if int(touch_candle["time"]) <= start_time:
                continue
            if float(touch_candle["low"]) <= zhigh and float(touch_candle["high"]) >= zlow:
                touched_value = True
                candidate_trigger = _v11_liquidity_trigger(c1, touch_idx, side)
                if candidate_trigger.get("ready") and int(candidate_trigger.get("reclaim_idx", -1)) == len(c1) - 1:
                    trigger = candidate_trigger
                    break
        if not trigger:
            last_reason = f"{side}: value zone touched={touched_value}, but no 1H sweep -> later reclaim completed on current candle"
            continue

        trigger_idx = int(trigger["reclaim_idx"])
        entry = float(c1[-1]["close"])
        atr4 = max(_safe_atr(c4), float(impulse["atr"]))
        atr1 = _safe_atr(c1)
        sweep_low = min(float(c["low"]) for c in c1[int(trigger["sweep_idx"]):trigger_idx + 1])
        sweep_high = max(float(c["high"]) for c in c1[int(trigger["sweep_idx"]):trigger_idx + 1])
        buffer = max(V11_STOP_BUFFER_ATR_4H * atr4, V11_STOP_BUFFER_ATR_1H * atr1)
        if V11_SL_MODE == "4H_ORIGIN":
            # Controlled experiment only: invalidate the completed 4H impulse,
            # rather than placing the stop inside the 1H sweep noise.
            if side == "LONG":
                stop = float(impulse["low"]) - buffer
            else:
                stop = float(impulse["high"]) + buffer
        elif side == "SWEEP":
            if side == "LONG":
                stop = sweep_low - buffer
            else:
                stop = sweep_high + buffer
        else:  # Defensive fallback; V11_SL_MODE is normalized above.
            stop = sweep_low - buffer if side == "LONG" else sweep_high + buffer
        risk = abs(entry - stop)
        if risk <= 0 or not math.isfinite(risk):
            last_reason = f"{side}: invalid structural stop/risk"
            continue
        sl_atr = risk / atr4 if atr4 > 0 else 999.0
        if sl_atr < V11_MIN_SL_ATR_SANITY or sl_atr > V11_MAX_SL_ATR_SANITY:
            last_reason = f"{side}: SL distance {sl_atr:.2f} ATR outside {V11_MIN_SL_ATR_SANITY:.2f}-{V11_MAX_SL_ATR_SANITY:.2f} safety bounds"
            continue

        target = _v11_target(c4, c12, c1d, entry, side, impulse)
        if not target.get("ok"):
            last_reason = f"{side}: no structural target beyond entry"
            continue
        tp = float(target["price"])
        target_path = _v11_target_path(
            c4, c12, c1d, entry, tp, side,
            target_time=int(target.get("time")) if target.get("time") is not None else None,
        )
        if not target_path.get("clear"):
            last_reason = f"{side}: target path blocked ({target_path.get('reason') or 'HTF obstacle'})"
            continue
        if side == "LONG" and not (stop < entry < tp):
            last_reason = f"{side}: invalid geometry SL < Entry < TP"
            continue
        if side == "SHORT" and not (tp < entry < stop):
            last_reason = f"{side}: invalid geometry TP < Entry < SL"
            continue

        rr_gross = abs(tp - entry) / risk
        cost_price = entry * max(0.0, cost_pct)
        rr_net = (abs(tp - entry) - cost_price) / (risk + cost_price) if risk + cost_price > 0 else 0.0
        if rr_net < V11_MIN_RR:
            last_reason = f"{side}: post-cost RR {rr_net:.2f} < required {V11_MIN_RR:.2f} (gross {rr_gross:.2f})"
            continue

        atr_rank = _atr_percentile(c4)
        last = c1[-1]
        range1 = float(last["high"]) - float(last["low"])
        shock_ok = not (atr1 > 0 and range1 > V11_SHOCK_RANGE_ATR * atr1)
        if not shock_ok:
            last_reason = f"{side}: 1H shock candle exceeds {V11_SHOCK_RANGE_ATR:.2f} ATR"
            continue

        value_mid = (float(zone["low"]) + float(zone["high"])) / 2.0
        value_quality = 1.0 - min(1.0, abs(entry - value_mid) / max(0.5 * atr4, 1e-9))
        deep = float(zone.get("deep_low", zone["low"])) <= entry <= float(zone.get("deep_high", zone["high"]))
        trigger_quality = float(trigger["quality"])
        target_quality = _clamp(rr_gross / 2.5, 0, 1)
        context_quality = 1.0 if context.get("healthy") else 0.6
        impulse_quality = _clamp(float(impulse["leg_atr"]) / 4.0, 0, 1)
        volatility_quality = 1.0 if 5 <= atr_rank <= 98 else 0.5
        score, score_groups = _v11_score({
            "impulse": impulse_quality, "value": max(value_quality, 0.85 if deep else 0.0),
            "trigger": trigger_quality, "target": target_quality,
            "context": context_quality, "volatility": volatility_quality,
        })
        btc_ok, btc_reason = btc_filter_ok(side, btc_context or {}, is_btc=symbol.upper().startswith("BTC"))
        if not btc_ok:
            return {
                "side": side,
                "direction_ok": direction_ok,
                "context": context,
                "candidate": False,
                "reason": f"{side}: BTC context veto ({btc_reason})",
                "technical_gate_failures": [f"{side}: BTC context veto ({btc_reason})"],
                "rejection_stage": "BTC",
                "btc_filter_ok": False,
            }
        return {
            "side": side, "candidate": True, "direction_ok": True, "context": context,
            "impulse": impulse, "value_zone": zone, "trigger": trigger,
            "entry": entry, "stop_loss": stop, "tp": tp, "rr": rr_net, "rr_gross": rr_gross,
            "atr_4h": atr4, "atr_1h": atr1, "sl_atr": sl_atr,
            "tp_distance_atr": abs(tp - entry) / atr4 if atr4 > 0 else 0.0,
            "target": target, "target_path": target_path, "score": score, "score_groups": score_groups,
            "atr_percentile": atr_rank, "btc_filter_ok": True, "btc_filter_reason": btc_reason,
            "value_deep": deep, "value_quality": value_quality, "shock_ok": shock_ok,
            "setup_state": "TRIGGERED", "entry_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
            "technical_gate_failures": [], "rejection_stage": None,
            "diagnostic_key": impulse_diag_key + f":TRIGGER:{int(trigger.get('trigger_time') or 0)}",
        }
    return {
        "side": side,
        "direction_ok": direction_ok,
        "context": context,
        "candidate": False,
        "reason": last_reason,
        "technical_gate_failures": [last_reason],
        "rejection_stage": "SETUP",
        "diagnostic_key": locals().get("impulse_diag_key"),
    }

def _v11_empty(
    symbol: str,
    c1d: list[Candle],
    c12: list[Candle],
    c4: list[Candle],
    c1: list[Candle],
    reason: str,
    daily: dict[str, Any],
    btc_context: dict[str, Any] | None = None,
    *,
    side_failures: list[str] | None = None,
) -> dict[str, Any]:
    price = float(c1[-1]["close"])
    return {
        "symbol": symbol.upper(), "price": price, "setup": "NONE", "setup_candidate": "NONE",
        "regime_1d": daily.get("regime"), "daily_structure_1d": daily.get("structure"),
        "signal_engine_version": ENGINE_VERSION, "signal_basis": "V11.2 Balanced Trend Pullback + Value Re-entry + 1H Liquidity Sweep/Reclaim",
        "sl_mode": V11_SL_MODE,
        "primary_entry_timeframe": "1H", "setup_timeframe": "4H", "signal_candle_timeframe": "1H",
        "technical_candidate": False, "signal_blocked": True, "rejection_stage": "SETUP",
        "technical_gate_failures": list(side_failures or [reason]),
        "diagnostic_failures": list(side_failures or [reason]),
        "reasons": list(side_failures or [reason]),
        "direction_ok": False, "structure_ok": False, "setup_ok": False, "confirmation_ok": False,
        "entry_distance_ok": False, "volatility_ok": True, "confirmation_family_diversity_ok": True,
        "location_ok": False, "target_path_ok": False, "target_path_structural": False, "target_path_clear": False,
        "risk_ok": False, "trade_geometry_ok": False, "rr_ok": False, "shock_veto_ok": True,
        "btc_filter_ok": True, "btc_filter_reason": "not evaluated", "score": 0, "score_groups": {},
        "stage_status": {"1D_REGIME": False, "12H_BIAS": False, "4H_SETUP": False, "1H_TRIGGER": False, "RISK": False, "RR": False, "QUALITY": True, "BTC": True, "SHOCK": True},
        "stage_failures": {"SETUP": [reason]}, "entry": None, "stop_loss": None, "tp": None, "rr": None,
        "atr_4h": _safe_atr(c4), "atr_1h": _safe_atr(c1), "atr": _safe_atr(c1),
        "atr_percentile": _atr_percentile(c4), "rvol_1h": _relative_volume(c1, 20),
        "entry_mode": "MARKET", "limit_price": None, "intraday_max_hold_minutes": DEFAULT_MAX_HOLD_MINUTES,
        "candle_open_time": int(c1[-1]["time"]), "candle_close_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
        "candle_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"], "entry_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
        "btc_context": btc_context or {"ok": False},
    }


def analyze_candles(
    symbol: str,
    candles_1d: list,
    candles_12h: list | None,
    candles_4h: list,
    candles_1h: list,
    *,
    now_ms: Optional[int] = None,
    cache: Optional[dict[str, Any]] = None,
    btc_context: Optional[dict[str, Any]] = None,
    estimated_round_trip_cost_pct: float = 0.0015,
) -> dict[str, Any]:
    """V11 deterministic Trend Pullback / Value Re-entry engine.

    Decision point: completed 1H candle close.
    Execution baseline: next 1H open.
    Authoritative timeframes: 1D / 12H / 4H / 1H only.
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    c1d = closed_candle_rows(candles_1d or [], "1D", now)
    c4 = closed_candle_rows(candles_4h or [], "4H", now)
    c1 = closed_candle_rows(candles_1h or [], "1H", now)
    c12 = closed_candle_rows(candles_12h, "12H", now) if candles_12h is not None else synthesize_12h_from_4h(c4, now_ms=now)
    for c, tf, minimum in ((c1d, "1d", 210), (c12, "12h", 60), (c4, "4h", 180), (c1, "1h", 180)):
        ok, reason = _data_quality(c, tf, minimum)
        if not ok:
            raise ValueError(f"{symbol}: {reason}")
    daily = _v11_regime_1d(c1d)
    cost_pct = max(0.0, _num(estimated_round_trip_cost_pct, 0.0015))
    candidates: list[dict[str, Any]] = []
    side_failures: list[str] = []
    side_diagnostics: list[dict[str, Any]] = []
    for side in ("LONG", "SHORT"):
        result = _v11_analyze_side(symbol, side, c1d, c12, c4, c1, daily, cost_pct, btc_context)
        if result.get("candidate"):
            candidates.append(result)
            side_diagnostics.append({"side": side, "candidate": True, "primary_failure": None, "diagnostic_key": result.get("diagnostic_key")})
        else:
            primary = str(result.get("primary_rejection_reason") or result.get("reason") or f"{side}: rejected")
            side_failures.extend([primary])
            side_diagnostics.append({"side": side, "candidate": False, "primary_failure": primary, "diagnostic_key": result.get("diagnostic_key")})
    if not candidates:
        reasons = side_failures or ["no active V11 value-pullback trigger"]
        result = _v11_empty(symbol, c1d, c12, c4, c1, "; ".join(reasons), daily, btc_context, side_failures=reasons)
        result["side_diagnostics"] = side_diagnostics
        result["primary_rejection_reason"] = side_diagnostics[0]["primary_failure"] if side_diagnostics else reasons[0]
        return result
    # If both sides somehow qualify, rank by structural setup quality only; this is
    # not an accuracy score and is not used as a threshold.
    chosen = max(candidates, key=lambda x: (float(x["score"]), int(x["impulse"]["high_idx"] if x["side"] == "LONG" else x["impulse"]["low_idx"])))
    side = str(chosen["side"])
    trigger = chosen["trigger"]
    impulse = chosen["impulse"]
    context = chosen["context"]
    entry = float(chosen["entry"]); stop = float(chosen["stop_loss"]); tp = float(chosen["tp"])
    rr = float(chosen["rr"]); rr_gross = float(chosen["rr_gross"])
    risk = abs(entry - stop)
    target_ok = bool(chosen.get("target", {}).get("ok"))
    target_clear = bool((chosen.get("target_path") or {}).get("clear"))
    geometry_ok = bool((stop < entry < tp) if side == "LONG" else (tp < entry < stop))
    btc_ok = bool(chosen.get("btc_filter_ok", True))
    stage_status = {
        "1D_REGIME": True, "12H_BIAS": not bool(context.get("hostile")), "4H_SETUP": True,
        "1H_TRIGGER": True, "ENTRY_DISTANCE": True, "VOLATILITY": True,
        "CONFIRMATION_FAMILIES": True, "TARGET_PATH": target_ok and target_clear,
        "RISK": geometry_ok, "RR": rr >= V11_MIN_RR, "QUALITY": True,
        "BTC": btc_ok, "SHOCK": bool(chosen.get("shock_ok", True)),
    }
    stage_failures = {k: [] for k in stage_status}
    direction_ok = True
    confirmation_ok = True
    structure_ok = True
    setup_ok = True
    families = {
        "trend_context": "PASS" if context.get("healthy") else "SUPPORT",
        "value_location": "PASS" if chosen.get("value_deep") else "SUPPORT",
        "liquidity_reclaim": "PASS",
        "relative_volume": "PASS" if _num(trigger.get("rvol")) >= 1.20 else "SUPPORT",
        "volatility": "PASS" if 5 <= float(chosen.get("atr_percentile", 50)) <= 98 else "SUPPORT",
        "target_geometry": "PASS",
    }
    passed = sum(v == "PASS" for v in families.values())
    score = int(chosen["score"])
    target_tf = str(chosen["target"].get("timeframe") or "4H")
    target_reason = "nearest structural target; RR applied after target selection"
    reasons = [
        f"1D {daily.get('regime')} permission",
        f"4H impulse {float(impulse['leg_atr']):.2f} ATR",
        f"Value zone {float(chosen['value_zone']['low']):.6g}-{float(chosen['value_zone']['high']):.6g}",
        "1H liquidity sweep + reclaim",
        f"Structural TP {target_tf}; post-cost RR {rr:.2f}",
    ]
    return {
        "symbol": symbol.upper(), "price": float(c1[-1]["close"]), "setup": side, "setup_candidate": side,
        "regime_1d": daily.get("regime"), "trend_4h": "HH/HL" if side == "LONG" else "LH/LL",
        "bias_12h": context.get("status"), "daily_structure_1d": daily.get("structure"),
        "structure_12h": context.get("structure"), "structure_4h": impulse.get("structure_label", "HH/HL" if side == "LONG" else "LH/LL"),
        "protected_structure_4h": "BULLISH" if side == "LONG" else "BEARISH",
        "ema21_1d": daily.get("e21"), "ema50_1d": daily.get("e50"), "ema200_1d": daily.get("e200"),
        "ema21_12h": context.get("e21"), "ema50_12h": context.get("e50"),
        "ema21_4h": _safe_ema([float(c["close"]) for c in c4], 21), "ema50_4h": _safe_ema([float(c["close"]) for c in c4], 50),
        "ema21_1h": _safe_ema([float(c["close"]) for c in c1], 21), "ema50_1h": _safe_ema([float(c["close"]) for c in c1], 50),
        "rsi": _safe_rsi([float(c["close"]) for c in c1]), "rsi_1h_entry": _safe_rsi([float(c["close"]) for c in c1]),
        "atr": float(chosen["atr_1h"]), "atr_1h": float(chosen["atr_1h"]), "atr_4h": float(chosen["atr_4h"]),
        "atr_percentile": float(chosen["atr_percentile"]), "rvol": _relative_volume(c1, 20), "rvol_1h": float(trigger.get("rvol", 0.0)),
        "volume": volume_status(c1), "adx_1d": _adx(c1d), "ema50_slope_1d": daily.get("slope"),
        "estimated_round_trip_cost_pct": cost_pct, "futures_context": {"status": "NOT_CHECKED", "execution_ok": None},
        "futures_ok": None, "futures_execution_ok": None, "data_fresh": None,
        "signal_engine_version": ENGINE_VERSION,
        "signal_basis": "1D macro vote → 12H health → 4H impulse/pullback into value → 1H liquidity sweep/reclaim → next 1H open",
        "primary_entry_timeframe": "1H", "setup_timeframe": "4H", "signal_candle_timeframe": "1H",
        "intraday_max_hold_minutes": DEFAULT_MAX_HOLD_MINUTES, "trigger_side": side,
        "trigger_quality": float(trigger.get("quality", 0.0)), "trigger_quality_1h": float(trigger.get("quality", 0.0)),
        "trigger_type": "LIQUIDITY_SWEEP_RECLAIM", "trigger_reason": trigger.get("reason"),
        "trigger_close_location": float(trigger.get("close_location", 0.0)),
        "structure_quality_ok": True, "trade_geometry_ok": geometry_ok,
        "momentum_quality": 0.5, "volume_quality": _clamp(float(trigger.get("rvol", 0.0)) / 1.20, 0, 1),
        "volatility_quality": 1.0 if 5 <= float(chosen.get("atr_percentile", 50)) <= 98 else 0.5,
        "entry_efficiency": 1.0, "twelve_h_context_quality": 1.0 if context.get("healthy") else 0.6,
        "entry_mode": "MARKET", "limit_price": None, "entry_1h_ready": True,
        "shock_veto_ok": bool(chosen.get("shock_ok", True)), "shock_veto_reason": "OK",
        "score": score, "score_groups": chosen["score_groups"],
        "confirmation_families": families, "confirmation_families_passed": passed, "confirmation_families_available": len(families),
        "confirmation_family_diversity_ok": True,
        "direction_ok": direction_ok, "structure_ok": structure_ok, "setup_ok": setup_ok, "confirmation_ok": confirmation_ok,
        "momentum_ok": True, "volume_ok": True, "location_ok": bool(target_ok and target_clear), "volatility_ok": True,
        "risk_ok": geometry_ok, "rr_ok": rr >= V11_MIN_RR, "entry_distance_ok": True,
        "stage_status": stage_status, "stage_failures": stage_failures, "technical_candidate": bool(geometry_ok and rr >= V11_MIN_RR and btc_ok),
        "signal_blocked": False, "rejection_stage": None, "technical_gate_failures": [], "diagnostic_failures": [],
        "side_diagnostics": side_diagnostics, "primary_rejection_reason": None,
        "reasons": reasons,
        "entry": entry, "stop_loss": stop, "tp": tp, "rr": rr, "rr_gross": rr_gross,
        "sl_atr": risk / float(chosen["atr_4h"]) if chosen["atr_4h"] > 0 else 0.0,
        "sl_atr_1h": risk / float(chosen["atr_1h"]) if chosen["atr_1h"] > 0 else 0.0,
        "sl_atr_4h": risk / float(chosen["atr_4h"]) if chosen["atr_4h"] > 0 else 0.0,
        "stop_distance_pct": risk / entry if entry > 0 else 0.0,
        "tp_distance_atr": abs(tp - entry) / float(chosen["atr_4h"]) if chosen["atr_4h"] > 0 else 0.0,
        "tp_distance_atr_1h": abs(tp - entry) / float(chosen["atr_1h"]) if chosen["atr_1h"] > 0 else 0.0,
        "tp_distance_pct": abs(tp - entry) / entry if entry > 0 else 0.0,
        "target_path_ok": bool(target_ok and target_clear),
        "target_path_structural": bool(target_ok),
        "target_path_clear": bool(target_clear),
        "target_path_reason": (chosen.get("target_path") or {}).get("reason") or target_reason,
        "target_path_obstacles": (chosen.get("target_path") or {}).get("obstacles") or [],
        "target_timeframe": target_tf, "target_levels": [chosen["target"]],
        "blocking_level": None,
        "stop_source": (
            "4H impulse-origin structural invalidation + volatility buffer"
            if V11_SL_MODE == "4H_ORIGIN"
            else "liquidity-sweep structural invalidation + volatility buffer"
        ),
        "sl_mode": V11_SL_MODE,
        "geometry_reason": "OK", "entry_limit_price": None, "limit_entry_expiry_minutes": 0,
        "candle_open_time": int(c1[-1]["time"]), "candle_close_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
        "candle_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
        "entry_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
        "trigger_candle_open_time": trigger.get("trigger_time"),
        "setup_impulse_high_time": impulse.get("high_time"), "setup_impulse_low_time": impulse.get("low_time"),
        "setup_pullback_touch_time": int(c1[int(trigger["sweep_idx"])]["time"]),
        "setup_retest_time": trigger.get("trigger_time"), "setup_retest_1h_time": trigger.get("trigger_time"),
        "setup_bos_time": None, "setup_departure_time": None,
        "value_zone_low": float(chosen["value_zone"]["low"]), "value_zone_high": float(chosen["value_zone"]["high"]),
        "value_deep": bool(chosen.get("value_deep")), "swept_level_1h": float(trigger["swept_level"]),
        "sweep_time_1h": trigger.get("sweep_time"), "reclaim_time_1h": trigger.get("trigger_time"),
        "rolling_vwap_12h": None, "flow_proxy_ratio": None,
        "closed_1d_candles": len(c1d), "closed_12h_candles": len(c12), "closed_4h_candles": len(c4), "closed_1h_candles": len(c1),
        "btc_filter_ok": btc_ok, "btc_filter_reason": chosen.get("btc_filter_reason", "OK"), "btc_would_block": not btc_ok,
        "btc_risk_mode": "OBSERVE", "btc_context": btc_context or {"ok": False},
    }






def _backtest_flow_proxy(candles: list[Candle], window: int = 12) -> float | None:
    rows = candles[-window:] if len(candles) > window else candles
    buy = sell = 0.0
    for c in rows:
        vol = _num(c.get("volume"))
        if _num(c.get("close")) > _num(c.get("open")):
            buy += vol
        elif _num(c.get("close")) < _num(c.get("open")):
            sell += vol
    total = buy + sell
    return (buy - sell) / total if total > 0 else None


def _rolling_vwap(candles: list[Candle], window: int = 48) -> float | None:
    rows = candles[-window:] if len(candles) > window else candles
    total = 0.0
    weighted = 0.0
    for c in rows:
        vol = _num(c.get("volume"))
        if vol <= 0:
            continue
        typical = (_num(c.get("high")) + _num(c.get("low")) + _num(c.get("close"))) / 3.0
        total += vol
        weighted += typical * vol
    return weighted / total if total > 0 else None






def build_btc_context(
    candles_1d: list,
    candles_12h: list | None,
    candles_4h: list,
    candles_1h: list | None = None,
) -> dict[str, Any]:
    """Build causal BTC context using only the approved V11 timeframes."""
    try:
        c1d = _coerce_candles(candles_1d)
        c12 = _coerce_candles(candles_12h or [])
        c4 = _coerce_candles(candles_4h)
        c1 = _coerce_candles(candles_1h or [])
        daily = _v11_regime_1d(c1d)
        structure_12h = _v11_structure(c12, 3, 3) if c12 else "UNKNOWN"
        structure_4h = _v11_structure(c4, 3, 3) if c4 else "UNKNOWN"
        a4 = _safe_atr(c4)
        a1 = _safe_atr(c1)
        move4 = (float(c4[-1]["close"]) - float(c4[-2]["close"])) / a4 if len(c4) >= 2 and a4 > 0 else 0.0
        move1 = (float(c1[-1]["close"]) - float(c1[-2]["close"])) / a1 if len(c1) >= 2 and a1 > 0 else 0.0
        return {
            "ok": True,
            "bull_1d": bool(daily.get("bull")),
            "bear_1d": bool(daily.get("bear")),
            "bull_4h": structure_4h == "HH/HL",
            "bear_4h": structure_4h == "LH/LL",
            "structure_12h": structure_12h,
            "structure_4h": structure_4h,
            "move_4h_atr": move4,
            "move_1h_atr": move1,
            "candle_time_4h": int(c4[-1]["time"]) if c4 else 0,
            "candle_time_1h": int(c1[-1]["time"]) if c1 else 0,
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def btc_filter_ok(side: str, context: dict[str, Any], *, is_btc: bool = False) -> tuple[bool, str]:
    if is_btc:
        return True, "BTC self-filter"
    if not context or not context.get("ok"):
        return True, "BTC context unavailable; filter abstained"
    side = str(side).upper()
    move4 = _num(context.get("move_4h_atr"))
    if side == "LONG":
        if context.get("bear_1d") and context.get("bear_4h"):
            return False, "BTC 1D and 4H regime is bearish against LONG"
        if move4 <= -BTC_SHOCK_ATR:
            return False, "BTC 4H shock against LONG"
    elif side == "SHORT":
        if context.get("bull_1d") and context.get("bull_4h"):
            return False, "BTC 1D and 4H regime is bullish against SHORT"
        if move4 >= BTC_SHOCK_ATR:
            return False, "BTC 4H shock against SHORT"
    else:
        return False, "Invalid side"
    return True, "OK"


async def analyze_symbol(market, symbol: str) -> dict[str, Any]:
    ref = await market.resolve(symbol)
    c1d = await market.ohlcv(ref, "1D", 250)
    c4 = await market.ohlcv(ref, "4H", 650)
    c1 = await market.ohlcv(ref, "1H", 250)
    c12 = synthesize_12h_from_4h(c4)
    return analyze_candles(ref.symbol, c1d, c12, c4, c1)
