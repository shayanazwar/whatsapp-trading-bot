from __future__ import annotations

"""Deterministic MEXC Futures signal engine.

Authoritative analysis timeframes are exactly:
    1D -> 12H -> 4H -> 1H

MEXC Futures does not expose a native 12H kline interval, so 12H candles are
causally synthesized from three completed 4H candles.
"""

import math
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

MIN_SCORE = 65
MIN_RR = 2.0
MIN_SL_ATR = 1.0
MAX_SL_ATR = 3.5
MIN_TP_ATR = 2.0
MIN_ATR_PERCENTILE = 10.0
MAX_ATR_PERCENTILE = 98.0
BOS_BUFFER_ATR = 0.10
RETEST_TOLERANCE_ATR = 0.35
RETEST_PENETRATION_ATR = 0.65
RETEST_INVALIDATION_ATR = 0.20
MAX_SETUP_AGE_4H = 18  # 72h
MAX_RETEST_BARS_4H = 10
MAX_ENTRY_DISTANCE_ATR = 0.60  # measured from the 4H BOS level in 4H ATR
BTC_SHOCK_ATR = 2.0
ADX_TREND_MIN = 14.0
MIN_TRIGGER_BODY = 0.40
MIN_TRIGGER_CLOSE_LOCATION = 0.58
MIN_TRIGGER_RVOL = 0.70
MAX_TRIGGER_BARS_1H = 3
DEFAULT_MAX_HOLD_MINUTES = 12 * 60
ENGINE_VERSION = "gold-v8.0-1d-12h-4h-1h-retest-geometry"

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
            if candle["low"] > candle["high"] or not (candle["low"] <= candle["close"] <= candle["high"]):
                continue
            output.append(candle)
        except (TypeError, ValueError, OverflowError, KeyError):
            continue
    dedup: dict[int, Candle] = {}
    for candle in sorted(output, key=lambda x: int(x["time"])):
        dedup[int(candle["time"])] = candle
    return [dedup[k] for k in sorted(dedup)]


def closed_candle_rows(candles: Iterable[Any] | None, timeframe_ms: Any, now_ms: Optional[int] = None) -> List[Candle]:
    interval = _timeframe_ms(timeframe_ms)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    source = candles if isinstance(candles, list) and (not candles or isinstance(candles[0], Candle)) else convert_candles(candles)
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


def _protected_structure(candles: list[Candle]) -> dict[str, Any]:
    highs, lows = _swing_points(candles)
    state = _structure_from_swings(highs, lows)
    return {
        "state": "BULLISH" if state == "HH/HL" else "BEARISH" if state == "LH/LL" else "NEUTRAL",
        "protected_high": highs[-1][1] if highs else None,
        "protected_low": lows[-1][1] if lows else None,
    }


def _macro_regime(candles: list[Candle]) -> dict[str, Any]:
    closes = [float(c["close"]) for c in candles]
    e21, e50, e200 = (_safe_ema(closes, p) for p in (21, 50, 200))
    structure = _structure_from_swings(*_swing_points(candles))
    slope = _ema_slope(closes, 50)
    adx = _adx(candles)
    if None in (e21, e50, e200):
        return {"regime": "INSUFFICIENT_DATA", "bull": False, "bear": False, "structure": structure, "e21": e21, "e50": e50, "e200": e200, "slope": slope, "adx": adx}
    price = closes[-1]
    bull_score = sum((price > e200, e21 > e50, structure == "HH/HL", slope > 0, adx >= ADX_TREND_MIN))
    bear_score = sum((price < e200, e21 < e50, structure == "LH/LL", slope < 0, adx >= ADX_TREND_MIN))
    bull = bull_score >= 3 and bull_score > bear_score
    bear = bear_score >= 3 and bear_score > bull_score
    return {"regime": "BULLISH" if bull else "BEARISH" if bear else "SIDEWAYS", "bull": bull, "bear": bear, "structure": structure, "e21": e21, "e50": e50, "e200": e200, "slope": slope, "adx": adx, "bull_votes": bull_score, "bear_votes": bear_score}


def _intermediate_bias(candles: list[Candle], daily: dict[str, Any]) -> dict[str, Any]:
    closes = [float(c["close"]) for c in candles]
    e21, e50 = _safe_ema(closes, 21), _safe_ema(closes, 50)
    structure = _structure_from_swings(*_swing_points(candles))
    slope = _ema_slope(closes, 34)
    r = _safe_rsi(closes)
    if e21 is None or e50 is None:
        return {"bull": False, "bear": False, "structure": "UNKNOWN", "e21": e21, "e50": e50, "slope": slope, "rsi": r, "votes": 0}
    bull_votes = sum((e21 > e50, structure == "HH/HL", slope > 0, r >= 50, not daily.get("bear")))
    bear_votes = sum((e21 < e50, structure == "LH/LL", slope < 0, r <= 50, not daily.get("bull")))
    return {"bull": bull_votes >= 3 and bull_votes > bear_votes, "bear": bear_votes >= 3 and bear_votes > bull_votes, "structure": structure, "e21": e21, "e50": e50, "slope": slope, "rsi": r, "bull_votes": bull_votes, "bear_votes": bear_votes, "votes": max(bull_votes, bear_votes)}


def _bos_strength(candle: Candle, level: float, atr_value: float) -> float:
    rng = max(float(candle["high"]) - float(candle["low"]), 1e-12)
    body = abs(float(candle["close"]) - float(candle["open"])) / rng
    displacement = abs(float(candle["close"]) - level) / atr_value if atr_value > 0 else 0.0
    return _clamp(0.55 * _clamp(body / 0.55, 0, 1) + 0.45 * _clamp(displacement / 0.50, 0, 1), 0, 1)


def _bos_events(candles: list[Candle], side: str, lookback: int = 70) -> list[dict[str, Any]]:
    if side not in {"LONG", "SHORT"} or len(candles) < 20:
        return []
    atr_values = _atr_series(candles)
    swings = _swing_points(candles)
    pivots = swings[0] if side == "LONG" else swings[1]
    indices = [x[0] for x in pivots]
    events: list[dict[str, Any]] = []
    start = max(1, len(candles) - lookback)
    for i in range(start, len(candles)):
        a = atr_values[i] if i < len(atr_values) else 0.0
        if a <= 0:
            continue
        eligible = bisect_right(indices, i - 2) - 1
        if eligible < 0:
            continue
        close, prev = float(candles[i]["close"]), float(candles[i - 1]["close"])
        buffer = max(BOS_BUFFER_ATR * a, 1e-12)
        for pos in range(eligible, -1, -1):
            idx, level = pivots[pos]
            crossed = prev <= level + buffer and close > level + buffer if side == "LONG" else prev >= level - buffer and close < level - buffer
            if crossed:
                events.append({"index": i, "time": int(candles[i]["time"]), "level": float(level), "atr": float(a), "strength": _bos_strength(candles[i], level, a), "swing_index": idx})
                break
    return events


def _pullback_retest(candles: list[Candle], side: str, bos: Optional[dict[str, Any]], max_bars: int = MAX_RETEST_BARS_4H) -> dict[str, Any]:
    empty = {"valid": False, "index": None, "time": None, "level": bos.get("level") if bos else None, "quality": 0.0, "rejection": False, "low": None, "high": None}
    if not bos:
        return empty
    start = int(bos["index"]) + 1
    end = min(len(candles), start + max_bars)
    a = _num(bos.get("atr"))
    if start >= end or a <= 0:
        return empty
    level = float(bos["level"])
    tol = max(a * RETEST_TOLERANCE_ATR, 1e-12)
    penetration = max(a * RETEST_PENETRATION_ATR, 1e-12)
    for i in range(start, end):
        c = candles[i]
        o, h, l, close = map(float, (c["open"], c["high"], c["low"], c["close"]))
        rng = max(h - l, 1e-12)
        if side == "LONG":
            # Once price closes materially back below the broken level before a
            # valid retest, the BOS is considered structurally failed.
            if close < level - a * RETEST_INVALIDATION_ATR:
                return empty
            intersects = l <= level + tol and h >= level - penetration
            held = close >= level - a * RETEST_INVALIDATION_ATR
            wick = min(o, close) - l
            directional = close >= o
        else:
            if close > level + a * RETEST_INVALIDATION_ATR:
                return empty
            intersects = h >= level - tol and l <= level + penetration
            held = close <= level + a * RETEST_INVALIDATION_ATR
            wick = h - max(o, close)
            directional = close <= o
        if not (intersects and held):
            continue

        # A retest is valid only while the broken level remains defended.
        # Look forward through the remaining setup window and invalidate the
        # BOS if price subsequently closes materially back through the level.
        invalidated = False
        for j in range(i + 1, end):
            future_close = float(candles[j]["close"])
            if (side == "LONG" and future_close < level - a * RETEST_INVALIDATION_ATR) or (side == "SHORT" and future_close > level + a * RETEST_INVALIDATION_ATR):
                invalidated = True
                break
        if invalidated:
            return empty

        rejection = wick / rng >= 0.18
        quality = _clamp(0.35 + (0.30 if rejection else 0) + (0.35 if directional else 0), 0, 1)
        return {"valid": True, "index": i, "time": int(c["time"]), "level": level, "quality": quality, "rejection": rejection, "low": l, "high": h}
    return empty


def _select_latest_bos_with_retest(candles: list[Candle], side: str, events: Optional[list[dict[str, Any]]] = None) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    events = events if events is not None else _bos_events(candles, side)
    latest = len(candles) - 1
    empty = _pullback_retest(candles, side, None)
    for bos in reversed(events):
        # Use a moderate structural-strength floor; the 1H trigger and risk model
        # provide the additional quality checks later in the pipeline.
        if _num(bos.get("strength")) < 0.60:
            continue
        if latest - int(bos["index"]) > MAX_SETUP_AGE_4H:
            continue
        retest = _pullback_retest(candles, side, bos, MAX_RETEST_BARS_4H)
        if retest["valid"] and latest - int(retest["index"]) <= MAX_SETUP_AGE_4H:
            return bos, retest
    return None, empty


def _one_hour_trigger_confirmation(candles: list[Candle], side: str, setup_level: Optional[float], retest_time: Optional[int]) -> dict[str, Any]:
    empty = {
        "ready": False, "quality": 0.0, "rsi": 50.0, "rvol": 0.0, "atr": 0.0,
        "body_ratio": 0.0, "close_location": 0.0, "candle_time": 0,
        "trigger_type": "NONE", "reason": "insufficient data", "bars_after_retest": None,
    }
    if len(candles) < 40 or setup_level is None or side not in {"LONG", "SHORT"}:
        return empty

    closes = [float(c["close"]) for c in candles]
    atr_value = _safe_atr(candles)
    if atr_value <= 0:
        empty["reason"] = "1H ATR unavailable"
        return empty

    latest_index = len(candles) - 1
    cur = candles[latest_index]
    if retest_time is not None:
        retest_close_time = int(retest_time) + TIMEFRAME_MS["4h"]
        if int(cur["time"]) < retest_close_time:
            empty["candle_time"] = int(cur["time"])
            empty["reason"] = "No 1H trigger after completed 4H retest"
            return empty
        trigger_base = next((i for i, c in enumerate(candles) if int(c["time"]) >= retest_close_time), None)
        if trigger_base is None:
            empty["candle_time"] = int(cur["time"])
            empty["reason"] = "No 1H trigger window after completed 4H retest"
            return empty
        bars_after_retest = latest_index - (trigger_base - 1)
        if bars_after_retest < 1 or bars_after_retest > MAX_TRIGGER_BARS_1H:
            empty["candle_time"] = int(cur["time"])
            empty["bars_after_retest"] = bars_after_retest
            empty["reason"] = "Latest 1H bar is outside retest trigger window"
            return empty
    else:
        bars_after_retest = None

    prev = candles[latest_index - 1]
    o, h, l, close = map(float, (cur["open"], cur["high"], cur["low"], cur["close"]))
    ph, pl = float(prev["high"]), float(prev["low"])
    rng = max(h - l, 1e-12)
    body = abs(close - o) / rng
    atr_i = _safe_atr(candles) or atr_value
    r = _safe_rsi(closes)
    rv = _relative_volume(candles)
    loc = (close - l) / rng if side == "LONG" else (h - close) / rng
    touch_tolerance = max(0.20 * atr_i, 1e-12)

    if side == "LONG":
        touched = l <= setup_level + touch_tolerance
        held = close > setup_level + max(0.05 * atr_i, 1e-12)
        wick = min(o, close) - l
        directional = close > o
        rsi_ok = r >= 50.0
        qmom = _clamp((r - 47.0) / 20.0, 0, 1)
    else:
        touched = h >= setup_level - touch_tolerance
        held = close < setup_level - max(0.05 * atr_i, 1e-12)
        wick = h - max(o, close)
        directional = close < o
        rsi_ok = r <= 50.0
        qmom = _clamp((53.0 - r) / 20.0, 0, 1)

    wick_ratio = wick / rng
    rejection = wick_ratio >= 0.25
    trigger = bool(touched and held and directional and rejection)
    hard_execution = bool(
        trigger
        and body >= MIN_TRIGGER_BODY
        and loc >= MIN_TRIGGER_CLOSE_LOCATION
        and rv >= MIN_TRIGGER_RVOL
        and rsi_ok
    )

    quality = _clamp(
        0.30 * _clamp(body / 0.70, 0, 1)
        + 0.25 * _clamp(rv / 1.5, 0, 1)
        + 0.20 * qmom
        + 0.15 * _clamp(wick_ratio / 0.60, 0, 1)
        + 0.10 * (1.0 if loc >= MIN_TRIGGER_CLOSE_LOCATION else 0.0),
        0, 1,
    )

    if not trigger:
        reason = "Latest 1H bar is not a retest rejection trigger"
    elif not hard_execution:
        reason = "1H retest rejection found but candle quality below minimum"
    else:
        reason = "1H retest rejection confirmation"
    return {
        "index": latest_index,
        "ready": hard_execution,
        "quality": quality,
        "rsi": r,
        "rvol": rv,
        "atr": atr_i,
        "body_ratio": body,
        "close_location": loc,
        "candle_time": int(cur["time"]),
        "trigger_type": "RETEST_REJECTION" if trigger else "NONE",
        "reason": reason,
        "bars_after_retest": bars_after_retest,
        "touched_level": touched,
        "rejection_wick_ratio": wick_ratio,
        "previous_high": ph,
        "previous_low": pl,
    }


def _collect_structural_levels(frames: Iterable[tuple[str, list[Candle]]], entry: float) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    priority = {"1D": 4, "12H": 3, "4H": 2, "1H": 1}
    for timeframe, candles in frames:
        highs, lows = _swing_points(candles)
        for idx, price in highs[-20:]:
            if price > entry:
                raw.append({"price": float(price), "timeframe": timeframe, "index": idx, "kind": "RESISTANCE"})
        for idx, price in lows[-20:]:
            if price < entry:
                raw.append({"price": float(price), "timeframe": timeframe, "index": idx, "kind": "SUPPORT"})
    raw.sort(key=lambda x: x["price"])
    clustered: list[dict[str, Any]] = []
    for level in raw:
        if not clustered or abs(level["price"] - clustered[-1]["price"]) > max(entry * 0.001, 1e-12):
            clustered.append(level.copy())
        elif priority[level["timeframe"]] > priority[clustered[-1]["timeframe"]]:
            clustered[-1] = level.copy()
    return clustered


def _target_path(frames: Iterable[tuple[str, list[Candle]]], side: str, entry: float, stop: float, atr_value: float) -> dict[str, Any]:
    risk = abs(entry - stop)
    result = {
        "ok": False, "tp": None, "risk": risk, "structural": False,
        "target_levels": [], "target_timeframe": None, "blocking_level": None,
        "reason": "no target",
    }
    if risk <= 0 or atr_value <= 0:
        result["reason"] = "zero risk or ATR"
        return result

    levels = [
        x for x in _collect_structural_levels(frames, entry)
        if x["timeframe"] in {"1D", "12H", "4H"}
    ]
    result["target_levels"] = levels[:20]
    if not levels:
        result["reason"] = "no opposing higher-timeframe structural level"
        return result

    if side == "LONG":
        opposing = sorted((x for x in levels if x["price"] > entry), key=lambda x: x["price"])
    else:
        opposing = sorted((x for x in levels if x["price"] < entry), key=lambda x: x["price"], reverse=True)
    if not opposing:
        result["reason"] = "no opposing higher-timeframe structural level"
        return result

    nearest = opposing[0]
    distance = abs(float(nearest["price"]) - entry)
    result["blocking_level"] = nearest
    result["target_timeframe"] = nearest["timeframe"]

    minimum_distance = MIN_TP_ATR * atr_value
    if distance < minimum_distance:
        result["reason"] = f"nearest HTF obstacle is too close ({distance:.6g} < {minimum_distance:.6g})"
        return result

    cost_pct = 0.0015
    cost = entry * cost_pct
    net_rr = max(0.0, distance - cost) / (risk + cost)
    if net_rr < MIN_RR:
        result["reason"] = f"nearest HTF target RR {net_rr:.2f} < {MIN_RR:.2f}; no clear target path"
        return result

    result.update({
        "ok": True,
        "tp": float(nearest["price"]),
        "structural": True,
        "target_timeframe": nearest["timeframe"],
        "tp_level": nearest,
        "reason": "nearest unblocked higher-timeframe structural target",
    })
    return result


def calculate_trade_levels(data: dict[str, Any]) -> dict[str, Any]:
    side = str(data.get("setup") or "").upper()
    entry = _num(data.get("price"))
    atr_1h = _num(data.get("atr_1h", data.get("atr")))
    atr_4h = _num(data.get("atr_4h", data.get("atr")))
    protected_low = _num(data.get("protected_low")) if data.get("protected_low") is not None else None
    protected_high = _num(data.get("protected_high")) if data.get("protected_high") is not None else None
    retest = data.get("retest") or {}
    if side not in {"LONG", "SHORT"} or entry <= 0 or atr_1h <= 0 or atr_4h <= 0:
        return {"entry": entry, "stop_loss": None, "tp": None, "rr": None, "trade_geometry_ok": False, "target_path_ok": False, "target_path_structural": False, "stop_source": None, "sl_atr": 0.0, "tp_distance_atr": 0.0, "geometry_reason": "invalid side/entry/ATR"}
    buffer = max(0.25 * atr_4h, 0.10 * atr_1h)
    if side == "LONG":
        retest_low = _num(retest.get("low")) if retest.get("low") is not None else None
        bos_pivot = _num(data.get("bos_pivot_price")) if data.get("bos_pivot_price") is not None else None
        anchors = [x for x in (retest_low, bos_pivot, protected_low) if x is not None and 0 < x < entry]
        anchor = anchors[0] if anchors else entry - atr_4h
        stop = anchor - buffer
        if entry - stop < MIN_SL_ATR * atr_4h:
            stop = entry - MIN_SL_ATR * atr_4h
        stop_source = "4H retest/BOS pivot + ATR buffer"
    else:
        retest_high = _num(retest.get("high")) if retest.get("high") is not None else None
        bos_pivot = _num(data.get("bos_pivot_price")) if data.get("bos_pivot_price") is not None else None
        anchors = [x for x in (retest_high, bos_pivot, protected_high) if x is not None and x > entry]
        anchor = anchors[0] if anchors else entry + atr_4h
        stop = anchor + buffer
        if stop - entry < MIN_SL_ATR * atr_4h:
            stop = entry + MIN_SL_ATR * atr_4h
        stop_source = "4H retest/BOS pivot + ATR buffer"
    stop_atr = abs(entry - stop) / atr_4h
    path = _target_path(data.get("target_frames", []), side, entry, stop, atr_4h)
    tp = _num(path.get("tp")) if path.get("tp") is not None else None
    if tp is None:
        return {"entry": entry, "stop_loss": stop, "tp": None, "rr": None, "trade_geometry_ok": False, "target_path_ok": bool(path.get("ok")), "target_path_structural": bool(path.get("structural")), "stop_source": stop_source, "sl_atr": stop_atr, "tp_distance_atr": 0.0, "target_levels": path.get("target_levels", []), "target_timeframe": path.get("target_timeframe"), "blocking_level": path.get("blocking_level"), "target_path_reason": path.get("reason"), "geometry_reason": str(path.get("reason"))}
    reward = abs(tp - entry)
    rr_gross = reward / abs(entry - stop) if abs(entry - stop) > 0 else 0.0
    cost_pct = max(0.0, _num(data.get("estimated_round_trip_cost_pct"), 0.0015))
    cost = entry * cost_pct
    net_rr = max(0.0, reward - cost) / (abs(entry - stop) + cost) if abs(entry - stop) > 0 else 0.0
    tp_atr = reward / atr_4h
    geometry_ok = bool(side == "LONG" and stop < entry < tp or side == "SHORT" and tp < entry < stop) and MIN_SL_ATR <= stop_atr <= MAX_SL_ATR and tp_atr >= MIN_TP_ATR and net_rr >= MIN_RR and path.get("structural")
    return {"entry": entry, "stop_loss": stop, "tp": tp, "rr": net_rr, "rr_gross": rr_gross, "estimated_round_trip_cost_pct": cost_pct, "target_path_ok": bool(path.get("ok")), "target_path_structural": bool(path.get("structural")), "target_path_reason": path.get("reason"), "target_levels": path.get("target_levels", []), "target_timeframe": path.get("target_timeframe"), "blocking_level": path.get("blocking_level"), "stop_source": stop_source, "sl_atr": stop_atr, "stop_distance_pct": abs(entry - stop) / entry, "tp_distance_atr": tp_atr, "tp_distance_pct": reward / entry, "trade_geometry_ok": geometry_ok, "geometry_reason": "OK" if geometry_ok else "trade geometry failed"}


def _direction_aligned(side: str, daily: dict[str, Any], bias: dict[str, Any], primary_structure: str, bos: Optional[dict[str, Any]] = None) -> bool:
    bos_strength = _num((bos or {}).get("strength"))
    if side == "LONG":
        if daily.get("bear") or bias.get("bear") or primary_structure == "LH/LL":
            return False
        directional_bias = bool(daily.get("bull") or bias.get("bull") or _num(bias.get("bull_votes")) >= 3)
        recent_break = bos_strength >= 0.50
        return bool(directional_bias or recent_break) and primary_structure != "LH/LL"
    if side == "SHORT":
        if daily.get("bull") or bias.get("bull") or primary_structure == "HH/HL":
            return False
        directional_bias = bool(daily.get("bear") or bias.get("bear") or _num(bias.get("bear_votes")) >= 3)
        recent_break = bos_strength >= 0.50
        return bool(directional_bias or recent_break) and primary_structure != "HH/HL"
    return False


def _shock_veto(candles: list[Candle], side: str, atr_value: float) -> tuple[bool, str]:
    if not candles or atr_value <= 0:
        return False, "1H shock veto unavailable"
    c = candles[-1]
    rng = float(c["high"]) - float(c["low"])
    adverse = float(c["open"]) - float(c["close"]) if side == "LONG" else float(c["close"]) - float(c["open"])
    if rng / atr_value > 4.5:
        return False, f"1H shock range {rng / atr_value:.2f} ATR > 4.50"
    if adverse / atr_value > 2.0:
        return False, f"1H adverse shock body {adverse / atr_value:.2f} ATR > 2.00"
    return True, "OK"


def evaluate_confirmation_families(data: dict[str, Any]) -> dict[str, Any]:
    side = str(data.get("setup") or "").upper()
    momentum = _clamp(_num(data.get("momentum_quality")), 0, 1)
    rvol = max(0.0, _num(data.get("rvol_1h", data.get("rvol"))))
    atr_rank = _num(data.get("atr_percentile"), 50.0)
    vwap = _num(data.get("rolling_vwap_12h"), _num(data.get("price")))
    price = _num(data.get("price"))
    structure_quality = _clamp(_num(data.get("structure_quality")), 0, 1)
    trigger_quality = _clamp(_num(data.get("trigger_quality")), 0, 1)
    path_ok = bool(data.get("target_path_structural"))
    vwap_ok = bool((price >= vwap and side == "LONG") or (price <= vwap and side == "SHORT"))
    volatility_ok = bool(MIN_ATR_PERCENTILE <= atr_rank <= MAX_ATR_PERCENTILE)

    families = {
        "structure_quality": {"status": "PASS" if structure_quality >= 0.60 else "FAIL", "value": structure_quality},
        "entry_quality": {"status": "PASS" if trigger_quality >= 0.55 else "FAIL", "value": trigger_quality},
        "momentum": {"status": "PASS" if momentum >= 0.55 else "FAIL", "value": momentum},
        "relative_volume": {"status": "PASS" if rvol >= 1.0 else "FAIL", "value": rvol},
        "volatility_regime": {"status": "PASS" if volatility_ok else "FAIL", "value": atr_rank},
        "target_path": {"status": "PASS" if path_ok else "FAIL", "value": path_ok},
        "vwap_location": {"status": "PASS" if vwap_ok else "FAIL", "value": vwap},
    }
    passed = sum(1 for item in families.values() if item["status"] == "PASS")
    return {"families": families, "passed": passed, "available": len(families), "diversity_ok": passed >= 4, "supporting_quality_ok": passed >= 4}


def _build_score(
    *,
    structure_quality: float,
    trigger_quality: float,
    momentum_quality: float,
    volume_quality: float,
    volatility_quality: float,
    location_quality: float,
    risk_quality: float,
) -> tuple[int, dict[str, int]]:
    """Score only supporting evidence; mandatory gates are not double-counted."""
    groups = {
        "structure_quality": int(round(25 * _clamp(structure_quality, 0, 1))),
        "entry_quality": int(round(15 * _clamp(trigger_quality, 0, 1))),
        "momentum": int(round(15 * _clamp(momentum_quality, 0, 1))),
        "volume": int(round(10 * _clamp(volume_quality, 0, 1))),
        "volatility": int(round(10 * _clamp(volatility_quality, 0, 1))),
        "location": int(round(10 * _clamp(location_quality, 0, 1))),
        "risk_geometry": int(round(15 * _clamp(risk_quality, 0, 1))),
    }
    return max(0, min(100, sum(groups.values()))), groups


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


def build_btc_context(candles_1d: list, candles_12h: list, candles_4h: list, candles_1h: list) -> dict[str, Any]:
    try:
        daily = _macro_regime(convert_candles(candles_1d))
        bias = _intermediate_bias(convert_candles(candles_12h), daily)
        c4 = convert_candles(candles_4h)
        c1 = convert_candles(candles_1h)
        a = _safe_atr(c1)
        move = (float(c1[-1]["close"]) - float(c1[-2]["close"])) / a if len(c1) >= 2 and a > 0 else 0.0
        return {"ok": True, "bull_1d": bool(daily.get("bull")), "bear_1d": bool(daily.get("bear")), "bull_12h": bool(bias.get("bull")), "bear_12h": bool(bias.get("bear")), "structure_4h": _structure_from_swings(*_swing_points(c4)), "move_1h_atr": move, "candle_time_1h": int(c1[-1]["time"]) if c1 else 0}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def btc_filter_ok(side: str, context: dict[str, Any], *, is_btc: bool = False) -> tuple[bool, str]:
    if is_btc:
        return True, "BTC self-filter"
    if not context or not context.get("ok"):
        return True, "BTC context unavailable; filter abstained"
    side = str(side).upper()
    move = _num(context.get("move_1h_atr"))
    if side == "LONG":
        if context.get("bear_1d") or context.get("bear_12h") or context.get("structure_4h") == "LH/LL":
            return False, "BTC higher-timeframe bias is bearish against LONG"
        if move <= -BTC_SHOCK_ATR:
            return False, "BTC 1H shock against LONG"
    elif side == "SHORT":
        if context.get("bull_1d") or context.get("bull_12h") or context.get("structure_4h") == "HH/HL":
            return False, "BTC higher-timeframe bias is bullish against SHORT"
        if move >= BTC_SHOCK_ATR:
            return False, "BTC 1H shock against SHORT"
    else:
        return False, "Invalid side"
    return True, "OK"


def calculate_confluence(data: dict[str, Any]) -> dict[str, Any]:
    return dict(data)


def _diagnostic_failures(data: dict[str, Any]) -> list[str]:
    checks = [
        (not data.get("direction_ok"), "1D/12H/4H direction"),
        (not data.get("structure_ok"), "4H BOS/retest structure"),
        (not data.get("setup_ok"), "4H setup side"),
        (not data.get("confirmation_ok"), "1H confirmation"),
        (not data.get("volatility_ok"), "1H volatility regime"),
        (not data.get("target_path_ok"), "HTF target path"),
        (not data.get("entry_distance_ok"), "1H entry distance"),
        (not data.get("risk_ok"), "structural risk/RR"),
        (not data.get("shock_veto_ok"), "shock veto"),
        (int(data.get("score", 0) or 0) < MIN_SCORE, f"quality score {int(data.get('score', 0) or 0)}<{MIN_SCORE}"),
    ]
    return list(dict.fromkeys(label for failed, label in checks if failed))


def analyze_candles(
    symbol: str,
    candles_1d: list,
    candles_12h: list | None,
    candles_4h: list,
    candles_1h: list,
    *,
    now_ms: Optional[int] = None,
    cache: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    c1d = closed_candle_rows(candles_1d or [], "1D", now)
    c4 = closed_candle_rows(candles_4h or [], "4H", now)
    c1 = closed_candle_rows(candles_1h or [], "1H", now)
    c12 = closed_candle_rows(candles_12h, "12H", now) if candles_12h is not None else synthesize_12h_from_4h(c4, now_ms=now)
    for c, tf, minimum in ((c1d, "1d", 210), (c12, "12h", 60), (c4, "4h", 180), (c1, "1h", 180)):
        ok, reason = _data_quality(c, tf, minimum)
        if not ok:
            raise ValueError(f"{symbol}: {reason}")

    daily = _macro_regime(c1d)
    bias = _intermediate_bias(c12, daily)
    primary_structure = _structure_from_swings(*_swing_points(c4))
    protected4 = _protected_structure(c4)
    bos_long, ret_long = _select_latest_bos_with_retest(c4, "LONG")
    bos_short, ret_short = _select_latest_bos_with_retest(c4, "SHORT")

    long_candidate = bool(bos_long and ret_long.get("valid"))
    short_candidate = bool(bos_short and ret_short.get("valid"))
    side = "LONG" if long_candidate and not short_candidate else "SHORT" if short_candidate and not long_candidate else "NONE"
    if side == "NONE" and long_candidate and short_candidate:
        side = "LONG" if _num(bos_long.get("strength")) >= _num(bos_short.get("strength")) else "SHORT"

    active_bos = bos_long if side == "LONG" else bos_short if side == "SHORT" else None
    active_retest = ret_long if side == "LONG" else ret_short if side == "SHORT" else {}
    level = _num(active_bos.get("level")) if active_bos else None
    trigger = _one_hour_trigger_confirmation(c1, side, level, active_retest.get("time")) if side in {"LONG", "SHORT"} else {"ready": False, "quality": 0.0, "rsi": 50.0, "rvol": 0.0, "atr": _safe_atr(c1), "body_ratio": 0.0, "close_location": 0.0, "candle_time": int(c1[-1]["time"]), "trigger_type": "NONE", "reason": "no active setup", "bars_after_retest": None}
    # Keep the structural side even when the 1H trigger is absent so risk/path diagnostics remain visible.
    setup = side if side in {"LONG", "SHORT"} else "NO TRADE"

    price = float(c1[-1]["close"])
    atr1 = _safe_atr(c1)
    atr4 = _safe_atr(c4)
    rsi1 = _safe_rsi([float(c["close"]) for c in c1])
    rvol1 = _relative_volume(c1)
    macd_line, macd_signal, macd_hist, macd_delta = _macd_components([float(c["close"]) for c in c1])
    momentum_quality = _clamp(((rsi1 - 50.0) / 20.0) if setup == "LONG" else ((50.0 - rsi1) / 20.0) if setup == "SHORT" else 0.0, 0.0, 1.0)
    if (setup == "LONG" and macd_hist > 0) or (setup == "SHORT" and macd_hist < 0):
        momentum_quality = _clamp(momentum_quality + 0.20, 0, 1)
    if (setup == "LONG" and macd_delta >= 0) or (setup == "SHORT" and macd_delta <= 0):
        momentum_quality = _clamp(momentum_quality + 0.10, 0, 1)
    atr_rank = _atr_percentile(c1)
    volatility_ok = MIN_ATR_PERCENTILE <= atr_rank <= MAX_ATR_PERCENTILE and atr1 > 0

    direction_ok = _direction_aligned(side, daily, bias, primary_structure, active_bos)
    structure_quality = _clamp(0.60 * _num((active_bos or {}).get("strength")) + 0.40 * _num(active_retest.get("quality")), 0, 1) if active_bos and active_retest else 0.0
    structure_ok = bool(active_bos and active_retest.get("valid") and _num(active_bos.get("strength")) >= 0.60 and _num(active_retest.get("quality")) >= 0.65)
    confirmation_ok = bool(trigger.get("ready"))
    setup_ok = side in {"LONG", "SHORT"}
    macro_ok = bool(
        (daily.get("bull") and not daily.get("bear"))
        or (daily.get("bear") and not daily.get("bull"))
        or (daily.get("regime") == "SIDEWAYS" and (bias.get("bull") or bias.get("bear")))
    )

    frames = [("1D", c1d), ("12H", c12), ("4H", c4), ("1H", c1)]
    bos_pivot_price = None
    if active_bos and active_bos.get("swing_index") is not None:
        pivot_index = int(active_bos["swing_index"])
        if 0 <= pivot_index < len(c4):
            bos_pivot_price = float(c4[pivot_index]["low"] if side == "LONG" else c4[pivot_index]["high"])
    levels = calculate_trade_levels({"setup": setup, "price": price, "atr_1h": atr1, "atr_4h": atr4, "protected_low": protected4.get("protected_low"), "protected_high": protected4.get("protected_high"), "bos_pivot_price": bos_pivot_price, "retest": active_retest, "target_frames": frames, "estimated_round_trip_cost_pct": 0.0015})
    entry_distance_atr = abs(price - _num(active_retest.get("level"))) / atr4 if active_retest.get("level") is not None and atr4 > 0 else float("inf")
    entry_distance_ok = bool(active_retest and active_retest.get("level") is not None and entry_distance_atr <= MAX_ENTRY_DISTANCE_ATR)
    target_path_ok = bool(levels.get("target_path_ok") and levels.get("target_path_structural"))
    location_ok = bool(target_path_ok and entry_distance_ok and _num(levels.get("tp_distance_atr")) >= MIN_TP_ATR)
    risk_ok = bool(levels.get("trade_geometry_ok") and _num(levels.get("rr")) >= MIN_RR)
    shock_ok, shock_reason = _shock_veto(c1, setup, atr1) if setup in {"LONG", "SHORT"} else (True, "No active setup")

    volatility_quality = 0.0
    if atr1 > 0 and MIN_ATR_PERCENTILE <= atr_rank <= MAX_ATR_PERCENTILE:
        volatility_quality = 1.0 if 25 <= atr_rank <= 85 else 0.75
    volume_quality = _clamp(0.65 * _clamp(rvol1 / 1.5, 0, 1) + 0.35 * (1.0 if volume_status(c1) == "INCREASING" else 0.55 if volume_status(c1) == "NORMAL" else 0.25), 0, 1)
    location_quality = 1.0 if location_ok else 0.0
    rr = _num(levels.get("rr"))
    if levels.get("trade_geometry_ok") and rr > MIN_RR:
        location_quality = max(location_quality, _clamp((rr - MIN_RR) / 3.0 + 0.65, 0, 1))
    risk_quality = _clamp((rr / 4.0) if rr > 0 else 0.0, 0, 1) if risk_ok else 0.25 if levels.get("target_path_structural") else 0.0

    score, score_groups = _build_score(
        structure_quality=structure_quality,
        trigger_quality=_num(trigger.get("quality")),
        momentum_quality=momentum_quality,
        volume_quality=volume_quality,
        volatility_quality=volatility_quality,
        location_quality=location_quality,
        risk_quality=risk_quality,
    )
    technical_candidate = bool(setup in {"LONG", "SHORT"} and direction_ok and structure_ok and setup_ok and confirmation_ok and volatility_ok and location_ok and risk_ok and shock_ok and score >= MIN_SCORE)
    family_result = evaluate_confirmation_families({"setup": setup, "momentum_quality": momentum_quality, "rvol_1h": rvol1, "atr_percentile": atr_rank, "target_path_structural": levels.get("target_path_structural"), "rolling_vwap_12h": _rolling_vwap(c12), "price": price, "structure_quality_ok": structure_ok, "structure_quality": structure_quality, "trigger_quality": trigger.get("quality")})

    failures = _diagnostic_failures({"direction_ok": direction_ok, "structure_ok": structure_ok, "setup_ok": setup_ok, "confirmation_ok": confirmation_ok, "volatility_ok": volatility_ok, "location_ok": location_ok, "risk_ok": risk_ok, "shock_veto_ok": shock_ok, "score": score})
    reasons = []
    if daily.get("regime") != "SIDEWAYS": reasons.append(f"1D macro regime: {daily.get('regime')}")
    if bias.get("bull") or bias.get("bear"): reasons.append(f"12H bias aligned: {'BULLISH' if bias.get('bull') else 'BEARISH'}")
    if structure_ok: reasons.append(f"4H {side} BOS + retest confirmed")
    if confirmation_ok: reasons.append(f"1H {trigger.get('trigger_type')} confirmation")
    if location_ok: reasons.append("HTF target path acceptable")
    if risk_ok: reasons.append(f"Post-cost RR {float(levels.get('rr') or 0):.2f}")
    if not technical_candidate: reasons.append("Technical candidate gate failed")

    return {
        "symbol": symbol.upper(), "price": price, "setup": setup, "setup_candidate": side,
        "regime_1d": daily.get("regime"), "trend_4h": daily.get("regime"), "bias_12h": "BULLISH" if bias.get("bull") else "BEARISH" if bias.get("bear") else "SIDEWAYS",
        "daily_structure_1d": daily.get("structure"), "structure_12h": bias.get("structure"), "structure_4h": primary_structure, "structure_1h": "HH/HL" if trigger.get("rsi", 50) >= 50 and setup == "LONG" else "LH/LL" if setup == "SHORT" else "RANGE",
        "protected_structure_4h": protected4.get("state"), "protected_high": protected4.get("protected_high"), "protected_low": protected4.get("protected_low"),
        "one_hour_long_votes": int(bias.get("bull_votes", 0)), "one_hour_short_votes": int(bias.get("bear_votes", 0)),
        "bos_4h": bool(active_bos), "bos_4h_time": active_bos.get("time") if active_bos else None, "bos_4h_strength": _num((active_bos or {}).get("strength")),
        "long_bos_level": bos_long.get("level") if bos_long else None, "short_bos_level": bos_short.get("level") if bos_short else None,
        "long_retest": bool(ret_long.get("valid")), "short_retest": bool(ret_short.get("valid")), "long_retest_time": ret_long.get("time"), "short_retest_time": ret_short.get("time"), "retest": active_retest or {},
        "ema21_1d": daily.get("e21"), "ema50_1d": daily.get("e50"), "ema200_1d": daily.get("e200"), "ema21_12h": bias.get("e21"), "ema50_12h": bias.get("e50"), "ema21_4h": _safe_ema([float(c["close"]) for c in c4], 21), "ema50_4h": _safe_ema([float(c["close"]) for c in c4], 50), "ema21_1h": _safe_ema([float(c["close"]) for c in c1], 21), "ema50_1h": _safe_ema([float(c["close"]) for c in c1], 50),
        "ema_direction": "BULLISH" if _num(_safe_ema([float(c["close"]) for c in c1], 21)) >= _num(_safe_ema([float(c["close"]) for c in c1], 50)) else "BEARISH",
        "rsi": rsi1, "rsi_1h_entry": trigger.get("rsi", rsi1), "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist, "macd_hist_delta": macd_delta,
        "atr": atr1, "atr_1h": atr1, "atr_4h": atr4, "atr_pct": atr1 / price if price > 0 else 0.0, "atr_percentile": atr_rank,
        "adx_1d": daily.get("adx"), "ema50_slope_1d": daily.get("slope"), "volume": volume_status(c1), "rvol": rvol1, "rvol_1h": rvol1,
        "support": max([x["price"] for x in _collect_structural_levels(frames, price) if x["price"] < price], default=None), "resistance": min([x["price"] for x in _collect_structural_levels(frames, price) if x["price"] > price], default=None),
        "futures_context": "PENDING", "futures_ok": True, "btc_filter_ok": False, "btc_filter_reason": "PENDING", "data_fresh": True,
        "signal_engine_version": ENGINE_VERSION, "signal_basis": "1D macro + 12H bias + 4H BOS/retest + 1H confirmation + structural SL/TP/RR", "primary_entry_timeframe": "1H", "setup_timeframe": "4H", "signal_candle_timeframe": "1H",
        "intraday_max_hold_minutes": DEFAULT_MAX_HOLD_MINUTES, "trigger_side": side, "trigger_quality": trigger.get("quality", 0.0), "trigger_type": trigger.get("trigger_type", "NONE"), "trigger_reason": trigger.get("reason", ""), "trigger_close_location": trigger.get("close_location", 0.0), "structure_quality_ok": structure_ok,
        "momentum_quality": momentum_quality, "volume_quality": volume_quality, "entry_1h_ready": bool(trigger.get("ready")), "shock_veto_ok": shock_ok, "shock_veto_reason": shock_reason,
        "score": score, "score_groups": score_groups, "confirmation_families": family_result.get("families", {}), "confirmation_families_passed": int(family_result.get("passed", 0)), "confirmation_families_available": int(family_result.get("available", 0)), "confirmation_family_diversity_ok": bool(family_result.get("diversity_ok")),
        "direction_ok": direction_ok, "structure_ok": structure_ok, "setup_ok": setup_ok, "confirmation_ok": confirmation_ok, "momentum_ok": momentum_quality >= 0.45, "volume_ok": volume_quality >= 0.50, "location_ok": location_ok, "volatility_ok": volatility_ok, "risk_ok": risk_ok,
        "stage_status": {
            "1D_REGIME": bool(daily.get("bull") or daily.get("bear") or daily.get("regime") == "SIDEWAYS"),
            "12H_BIAS": bool(bias.get("bull") or bias.get("bear")),
            "4H_SETUP": bool(active_bos and active_retest.get("valid")),
            "1H_TRIGGER": bool(trigger.get("ready")),
            "QUALITY": bool(score >= MIN_SCORE),
            "RISK": bool(risk_ok),
            "RR": bool(rr >= MIN_RR),
        },
        "stage_failures": {
            "1D_REGIME": [] if (daily.get("bull") or daily.get("bear") or daily.get("regime") == "SIDEWAYS") else [str(daily.get("regime") or "unknown")],
            "12H_BIAS": [] if (bias.get("bull") or bias.get("bear")) else ["no directional 12H bias"],
            "4H_SETUP": [] if (active_bos and active_retest.get("valid")) else ["no confirmed 4H BOS/retest"],
            "1H_TRIGGER": [] if trigger.get("ready") else [str(trigger.get("reason") or "no 1H trigger")],
            "QUALITY": [] if score >= MIN_SCORE else [f"score {score} < {MIN_SCORE}"],
            "RISK": [] if risk_ok else [str(levels.get("geometry_reason") or "risk geometry failed")],
            "RR": [] if rr >= MIN_RR else [f"RR {rr:.2f} < {MIN_RR:.2f}"],
        },
        "technical_candidate": technical_candidate, "signal_blocked": not technical_candidate, "rejection_stage": None if technical_candidate else ("1H_TRIGGER" if setup_ok and structure_ok and direction_ok and not confirmation_ok else "4H_SETUP" if setup_ok and not structure_ok else "DIRECTION" if setup_ok else "SETUP"), "technical_gate_failures": failures, "diagnostic_failures": failures, "reasons": reasons,
        "entry": levels.get("entry"), "stop_loss": levels.get("stop_loss"), "tp": levels.get("tp"), "rr": levels.get("rr"), "rr_gross": levels.get("rr_gross"), "sl_atr": levels.get("sl_atr", 0.0), "stop_distance_pct": levels.get("stop_distance_pct", 0.0), "tp_distance_atr": levels.get("tp_distance_atr", 0.0), "tp_distance_pct": levels.get("tp_distance_pct", 0.0), "target_path_ok": levels.get("target_path_ok", False), "target_path_structural": levels.get("target_path_structural", False), "target_path_reason": levels.get("target_path_reason"), "target_timeframe": levels.get("target_timeframe"), "target_levels": levels.get("target_levels", []), "blocking_level": levels.get("blocking_level"), "entry_distance_atr": entry_distance_atr, "entry_distance_ok": entry_distance_ok, "stop_source": levels.get("stop_source"), "trade_geometry_ok": levels.get("trade_geometry_ok", False), "geometry_reason": levels.get("geometry_reason"),
        "candle_open_time": int(c1[-1]["time"]), "candle_close_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"], "candle_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"], "trigger_candle_open_time": trigger.get("candle_time"), "setup_bos_time": active_bos.get("time") if active_bos else None, "setup_retest_time": active_retest.get("time") if active_retest else None,
        "rolling_vwap_12h": _rolling_vwap(c12), "flow_proxy_ratio": _backtest_flow_proxy(c1), "closed_1d_candles": len(c1d), "closed_12h_candles": len(c12), "closed_4h_candles": len(c4), "closed_1h_candles": len(c1),
    }


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


async def analyze_symbol(market, symbol: str) -> dict[str, Any]:
    ref = await market.resolve(symbol)
    c1d = await market.ohlcv(ref, "1D", 250)
    c4 = await market.ohlcv(ref, "4H", 650)
    c1 = await market.ohlcv(ref, "1H", 250)
    c12 = synthesize_12h_from_4h(c4)
    return analyze_candles(ref.symbol, c1d, c12, c4, c1)
