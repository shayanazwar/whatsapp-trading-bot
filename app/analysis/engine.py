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
MIN_SL_ATR = 0.50
MAX_SL_ATR = 1.25
MIN_TP_ATR = 1.00
MIN_ATR_PERCENTILE = 5.0
MAX_ATR_PERCENTILE = 98.0
BOS_BUFFER_ATR = 0.10
RETEST_TOLERANCE_ATR = 0.25
RETEST_PENETRATION_ATR = 0.75
RETEST_INVALIDATION_ATR = 0.25
MAX_SETUP_AGE_4H = 18  # 72h
MAX_RETEST_BARS_4H = 12
MAX_ENTRY_DISTANCE_ATR = 0.40  # market-entry distance from retest zone edge
MAX_LIMIT_ENTRY_DISTANCE_ATR = 1.25
MIN_DEPARTURE_ATR = 0.75
MAX_DEPARTURE_BARS_4H = 6
MAX_RETEST_1H_BARS = 12
RETEST_ZONE_FLOOR_ATR = 0.25
RETEST_ZONE_CEILING_ATR = 0.75
RETEST_WICK_MIN = 0.30
LIMIT_ENTRY_EXPIRY_BARS = 2
BTC_SHOCK_ATR = 2.0
ADX_TREND_MIN = 14.0
MIN_TRIGGER_BODY = 0.25
MIN_TRIGGER_CLOSE_LOCATION = 0.58
MIN_TRIGGER_RVOL = 0.70
MAX_TRIGGER_BARS_1H = 3
DEFAULT_MAX_HOLD_MINUTES = 72 * 60
ENGINE_VERSION = "gold-v9.0-true-retest-geometry"

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
    empty = {
        "valid": False, "index": None, "time": None, "level": bos.get("level") if bos else None,
        "quality": 0.0, "rejection": False, "low": None, "high": None,
        "zone_floor": None, "zone_ceiling": None, "departure_index": None,
        "departure_time": None, "departure_atr": 0.0, "state": "IDLE",
        "atr": _num((bos or {}).get("atr")),
    }
    if not bos or side not in {"LONG", "SHORT"}:
        return empty
    bos_index = int(bos["index"])
    start = bos_index + 1
    end = min(len(candles), start + max_bars)
    atr4 = _num(bos.get("atr"))
    if start >= end or atr4 <= 0:
        return empty

    level = float(bos["level"])
    bos_candle = candles[bos_index]
    body_hi = max(float(bos_candle["open"]), float(bos_candle["close"]))
    body_lo = min(float(bos_candle["open"]), float(bos_candle["close"]))
    if side == "LONG":
        zone_floor = level - RETEST_ZONE_FLOOR_ATR * atr4
        zone_ceiling = min(max(level, body_hi), level + RETEST_ZONE_CEILING_ATR * atr4)
    else:
        zone_floor = max(level - RETEST_ZONE_CEILING_ATR * atr4, min(level, body_lo))
        zone_ceiling = level + RETEST_ZONE_FLOOR_ATR * atr4

    departed = False
    departure_index = None
    departure_atr = 0.0
    touch_index = None
    touch_low = None
    touch_high = None

    for i in range(start, end):
        c = candles[i]
        h, l, close = float(c["high"]), float(c["low"]), float(c["close"])

        # A real retest must first have a meaningful displacement away from L.
        if not departed:
            if side == "LONG":
                if close <= level and h < level + MIN_DEPARTURE_ATR * atr4:
                    if close < zone_floor:
                        return empty
                    continue
                if h >= level + MIN_DEPARTURE_ATR * atr4 and close > level:
                    departed = True
                    departure_index = i
                    departure_atr = (h - level) / atr4
                    continue
                if close < zone_floor:
                    return empty
                continue
            else:
                if close >= level and l > level - MIN_DEPARTURE_ATR * atr4:
                    if close > zone_ceiling:
                        return empty
                    continue
                if l <= level - MIN_DEPARTURE_ATR * atr4 and close < level:
                    departed = True
                    departure_index = i
                    departure_atr = (level - l) / atr4
                    continue
                if close > zone_ceiling:
                    return empty
                continue

        # After departure, a setup may not run endlessly without retesting.
        if touch_index is None:
            if i - int(departure_index) > MAX_RETEST_BARS_4H:
                return empty
            if side == "LONG" and h >= level + 3.0 * atr4:
                return empty
            if side == "SHORT" and l <= level - 3.0 * atr4:
                return empty
            touched = l <= zone_ceiling and h >= zone_floor
            if touched:
                if side == "LONG" and close < zone_floor:
                    return empty
                if side == "SHORT" and close > zone_ceiling:
                    return empty
                touch_index = i
                touch_low, touch_high = l, h
            continue

        touch_low = min(float(touch_low), l)
        touch_high = max(float(touch_high), h)
        if side == "LONG" and close < zone_floor:
            return empty
        if side == "SHORT" and close > zone_ceiling:
            return empty
        if i - int(touch_index) > max_bars:
            break

    if not departed or departure_index is None or touch_index is None:
        return empty

    tc = candles[touch_index]
    rng = max(float(tc["high"]) - float(tc["low"]), 1e-12)
    if side == "LONG":
        wick = min(float(tc["open"]), float(tc["close"])) - float(tc["low"])
        directional = float(tc["close"]) >= float(tc["open"])
    else:
        wick = float(tc["high"]) - max(float(tc["open"]), float(tc["close"]))
        directional = float(tc["close"]) <= float(tc["open"])
    rejection = wick / rng >= RETEST_WICK_MIN
    quality = _clamp(0.55 + (0.25 if rejection else 0.0) + (0.20 if directional else 0.0), 0, 1)
    return {
        "valid": True,
        "index": touch_index,
        "time": int(candles[touch_index]["time"]),
        "level": level,
        "quality": quality,
        "rejection": rejection,
        "low": float(touch_low),
        "high": float(touch_high),
        "zone_floor": float(zone_floor),
        "zone_ceiling": float(zone_ceiling),
        "departure_index": departure_index,
        "departure_time": int(candles[departure_index]["time"]),
        "departure_atr": float(departure_atr),
        "atr": atr4,
        "state": "RETEST_TOUCHED",
    }

def _select_latest_bos_with_retest(candles: list[Candle], side: str, events: Optional[list[dict[str, Any]]] = None) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    events = events if events is not None else _bos_events(candles, side)
    latest = len(candles) - 1
    empty = _pullback_retest(candles, side, None)
    for bos in reversed(events):
        # Use a moderate structural-strength floor; the 1H trigger and risk model
        # provide the additional quality checks later in the pipeline.
        if _num(bos.get("strength")) < 0.50:
            continue
        if latest - int(bos["index"]) > MAX_SETUP_AGE_4H:
            continue
        retest = _pullback_retest(candles, side, bos, MAX_RETEST_BARS_4H)
        if retest["valid"] and latest - int(retest["index"]) <= MAX_SETUP_AGE_4H:
            return bos, retest
    return None, empty


def _one_hour_trigger_confirmation(
    candles: list[Candle],
    side: str,
    setup: Optional[dict[str, Any]] = None,
    retest_time: Optional[int] = None,
) -> dict[str, Any]:
    # Backward compatibility: callers passing a bare level + retest time still work,
    # but the production path supplies the full retest zone dictionary.
    setup_dict = setup if isinstance(setup, dict) else {"level": setup, "time": retest_time}
    empty = {
        "ready": False, "quality": 0.0, "rsi": 50.0, "rvol": 0.0, "atr": 0.0,
        "body_ratio": 0.0, "close_location": 0.0, "candle_time": 0,
        "trigger_type": "NONE", "reason": "insufficient data", "bars_after_retest": None,
        "touched_level": False, "rejection_wick_ratio": 0.0, "entry_price": None,
        "entry_mode": "MARKET", "limit_price": None, "zone_floor": None, "zone_ceiling": None,
        "retest_low_1h": None, "retest_high_1h": None,
    }
    if len(candles) < 40 or side not in {"LONG", "SHORT"}:
        return empty
    level = _num(setup_dict.get("level"))
    zone_floor = _num(setup_dict.get("zone_floor"))
    zone_ceiling = _num(setup_dict.get("zone_ceiling"))
    if zone_floor <= 0 or zone_ceiling <= 0 or level <= 0:
        if retest_time is None:
            empty["reason"] = "1H retest zone unavailable"
            return empty
        zone_floor = level - 0.25 * _safe_atr(candles)
        zone_ceiling = level + 0.25 * _safe_atr(candles)
    start_time = int(setup_dict.get("time") or retest_time or 0)
    atr4 = max(_num(setup_dict.get("atr")), _safe_atr(candles))
    atr1 = _safe_atr(candles)
    if level <= 0 or atr1 <= 0 or atr4 <= 0 or start_time <= 0:
        empty["reason"] = "1H retest zone/ATR unavailable"
        return empty

    latest_index = len(candles) - 1
    cur = candles[latest_index]
    empty["candle_time"] = int(cur["time"])
    closes = [float(c["close"]) for c in candles]
    r = _safe_rsi(closes)
    rv = _relative_volume(candles)
    o, h, l, close = map(float, (cur["open"], cur["high"], cur["low"], cur["close"]))
    rng = max(h - l, 1e-12)
    body = abs(close - o) / rng
    loc = (close - l) / rng if side == "LONG" else (h - close) / rng
    lower_wick = min(o, close) - l
    upper_wick = h - max(o, close)

    start_idx = next((i for i, c in enumerate(candles) if int(c["time"]) >= start_time), None)
    if start_idx is None:
        empty["reason"] = "No 1H candles after retest"
        return empty

    touched = False
    touch_idx = None
    extreme_low = None
    extreme_high = None
    invalid = False
    rejection_idx = None
    rejection_wick = 0.0

    for i in range(start_idx, latest_index + 1):
        c = candles[i]
        co, ch, cl, cc = map(float, (c["open"], c["high"], c["low"], c["close"]))
        if not touched:
            if cl <= zone_ceiling and ch >= zone_floor:
                touched = True
                touch_idx = i
                extreme_low = cl
                extreme_high = ch
                if side == "LONG" and cc < zone_floor:
                    invalid = True
                    break
                if side == "SHORT" and cc > zone_ceiling:
                    invalid = True
                    break
            continue

        extreme_low = min(float(extreme_low), cl)
        extreme_high = max(float(extreme_high), ch)
        if side == "LONG" and cc < zone_floor:
            invalid = True
            break
        if side == "SHORT" and cc > zone_ceiling:
            invalid = True
            break

        bars = i - int(touch_idx)
        if bars > MAX_RETEST_1H_BARS:
            break

        crange = max(ch - cl, 1e-12)
        if side == "LONG":
            wick = min(co, cc) - cl
            rejection = cc > level + max(0.03 * atr1, 1e-12) and (cc >= co or wick / crange >= RETEST_WICK_MIN)
        else:
            wick = ch - max(co, cc)
            rejection = cc < level - max(0.03 * atr1, 1e-12) and (cc <= co or wick / crange >= RETEST_WICK_MIN)
        if i == latest_index and rejection:
            rejection_idx = i
            rejection_wick = wick / crange

    if invalid:
        empty.update({"reason": "1H retest invalidated through zone floor", "touched_level": touched, "retest_low_1h": extreme_low, "retest_high_1h": extreme_high})
        return empty
    if not touched:
        empty["reason"] = "No 1H retest touch in breakout zone"
        return empty
    bars_after_retest = latest_index - int(touch_idx)
    if bars_after_retest > MAX_RETEST_1H_BARS:
        empty.update({"bars_after_retest": bars_after_retest, "touched_level": True, "reason": "1H retest confirmation window expired", "retest_low_1h": extreme_low, "retest_high_1h": extreme_high})
        return empty
    if rejection_idx is None:
        empty.update({"bars_after_retest": bars_after_retest, "touched_level": True, "reason": "Latest 1H bar is not a retest rejection/reclaim", "retest_low_1h": extreme_low, "retest_high_1h": extreme_high})
        return empty

    zone_edge = zone_ceiling if side == "LONG" else zone_floor
    current_distance_atr = abs(close - zone_edge) / atr4
    if current_distance_atr <= MAX_ENTRY_DISTANCE_ATR:
        entry_price = close
        entry_mode = "MARKET"
        limit_price = None
    elif current_distance_atr <= MAX_LIMIT_ENTRY_DISTANCE_ATR:
        entry_price = zone_edge
        entry_mode = "LIMIT"
        limit_price = zone_edge
    else:
        empty.update({"reason": f"1H confirmation too far from retest zone ({current_distance_atr:.2f} ATR4H)", "bars_after_retest": bars_after_retest, "touched_level": True, "retest_low_1h": extreme_low, "retest_high_1h": extreme_high})
        return empty

    momentum_quality = _clamp(((r - 50.0) / 20.0) if side == "LONG" else ((50.0 - r) / 20.0), 0, 1)
    quality = _clamp(
        0.30 * _clamp(body / 0.70, 0, 1)
        + 0.20 * _clamp(rv / 1.5, 0, 1)
        + 0.20 * momentum_quality
        + 0.15 * _clamp(loc, 0, 1)
        + 0.15 * _clamp(rejection_wick / 0.60, 0, 1),
        0, 1,
    )
    return {
        "index": latest_index, "ready": True, "quality": quality, "rsi": r, "rvol": rv,
        "atr": atr1, "body_ratio": body, "close_location": loc, "candle_time": int(cur["time"]),
        "trigger_type": "RETEST_RECLAIM", "reason": "Latest 1H retest rejection/reclaim",
        "bars_after_retest": bars_after_retest, "touched_level": True,
        "rejection_wick_ratio": rejection_wick, "previous_high": float(candles[latest_index - 1]["high"]),
        "previous_low": float(candles[latest_index - 1]["low"]), "entry_price": entry_price,
        "entry_mode": entry_mode, "limit_price": limit_price, "zone_floor": zone_floor,
        "zone_ceiling": zone_ceiling, "retest_low_1h": extreme_low, "retest_high_1h": extreme_high,
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
        "ok": False, "tp": None, "risk": risk, "structural": False, "target_levels": [],
        "target_timeframe": None, "blocking_level": None, "reason": "no target",
    }
    if risk <= 0 or atr_value <= 0:
        result["reason"] = "zero risk or ATR"
        return result

    htf_frames = [(tf, candles) for tf, candles in frames if tf in {"1D", "12H", "4H"}]
    levels = [x for x in _collect_structural_levels(htf_frames, entry) if x.get("timeframe") in {"1D", "12H", "4H"}]
    if side == "LONG":
        levels = [x for x in levels if x["kind"] == "RESISTANCE" and float(x["price"]) > entry]
        levels.sort(key=lambda x: float(x["price"]))
    else:
        levels = [x for x in levels if x["kind"] == "SUPPORT" and float(x["price"]) < entry]
        levels.sort(key=lambda x: float(x["price"]), reverse=True)
    result["target_levels"] = levels[:20]
    if not levels:
        result["reason"] = "no meaningful HTF opposing structure"
        return result

    # The nearest meaningful HTF obstacle governs TP. Never skip it merely to
    # manufacture a higher planned RR.
    obstacle = levels[0]
    level_price = float(obstacle["price"])
    front_run = 0.15 * atr_value
    tp = level_price - front_run if side == "LONG" else level_price + front_run
    if side == "LONG" and tp <= entry:
        result.update({"reason": "nearest HTF resistance is too close", "blocking_level": obstacle})
        return result
    if side == "SHORT" and tp >= entry:
        result.update({"reason": "nearest HTF support is too close", "blocking_level": obstacle})
        return result
    result.update({
        "ok": True, "tp": tp, "structural": True, "target_timeframe": obstacle["timeframe"],
        "tp_level": obstacle, "blocking_level": obstacle,
        "reason": "nearest meaningful higher-timeframe opposing level",
    })
    return result

def calculate_trade_levels(data: dict[str, Any]) -> dict[str, Any]:
    side = str(data.get("setup") or "").upper()
    price = _num(data.get("price"))
    entry = _num(data.get("entry_price", price))
    atr_1h = _num(data.get("atr_1h", data.get("atr")))
    atr_4h = _num(data.get("atr_4h", data.get("atr")))
    retest = data.get("retest") or {}
    entry_mode = str(data.get("entry_mode") or "MARKET").upper()
    if side not in {"LONG", "SHORT"} or entry <= 0 or atr_1h <= 0 or atr_4h <= 0:
        return {"entry": entry, "stop_loss": None, "tp": None, "rr": None, "trade_geometry_ok": False, "target_path_ok": False, "target_path_structural": False, "stop_source": None, "sl_atr": 0.0, "tp_distance_atr": 0.0, "geometry_reason": "invalid side/entry/ATR"}

    zone_floor = _num(retest.get("zone_floor"))
    zone_ceiling = _num(retest.get("zone_ceiling"))
    retest_low = _num(data.get("retest_low_1h", retest.get("low"))) if data.get("retest_low_1h", retest.get("low")) is not None else None
    retest_high = _num(data.get("retest_high_1h", retest.get("high"))) if data.get("retest_high_1h", retest.get("high")) is not None else None
    buffer = max(0.20 * atr_4h, 0.50 * atr_1h)

    if side == "LONG":
        anchors = [x for x in (retest_low, zone_floor) if x is not None and x > 0 and x < entry]
        if not anchors:
            return {"entry": entry, "stop_loss": None, "tp": None, "rr": None, "trade_geometry_ok": False, "target_path_ok": False, "target_path_structural": False, "stop_source": None, "sl_atr": 0.0, "tp_distance_atr": 0.0, "geometry_reason": "missing retest structural anchor"}
        anchor = min(anchors)
        stop = anchor - buffer
        min_dist = max(MIN_SL_ATR * atr_4h, 1.0 * atr_1h)
        if entry - stop < min_dist:
            stop = entry - min_dist
        stop_source = "1H retest extreme + 4H zone floor + ATR buffer"
    else:
        anchors = [x for x in (retest_high, zone_ceiling) if x is not None and x > 0 and x > entry]
        if not anchors:
            return {"entry": entry, "stop_loss": None, "tp": None, "rr": None, "trade_geometry_ok": False, "target_path_ok": False, "target_path_structural": False, "stop_source": None, "sl_atr": 0.0, "tp_distance_atr": 0.0, "geometry_reason": "missing retest structural anchor"}
        anchor = max(anchors)
        stop = anchor + buffer
        min_dist = max(MIN_SL_ATR * atr_4h, 1.0 * atr_1h)
        if stop - entry < min_dist:
            stop = entry + min_dist
        stop_source = "1H retest extreme + 4H zone ceiling + ATR buffer"

    sl_atr = abs(entry - stop) / atr_4h
    if sl_atr > MAX_SL_ATR:
        return {"entry": entry, "stop_loss": stop, "tp": None, "rr": None, "trade_geometry_ok": False, "target_path_ok": False, "target_path_structural": False, "stop_source": stop_source, "sl_atr": sl_atr, "tp_distance_atr": 0.0, "geometry_reason": f"SL distance {sl_atr:.2f} ATR4H exceeds maximum {MAX_SL_ATR:.2f}", "entry_mode": entry_mode}

    path = _target_path(data.get("target_frames", []), side, entry, stop, atr_4h)
    tp = _num(path.get("tp")) if path.get("tp") is not None else None
    if tp is None:
        return {"entry": entry, "stop_loss": stop, "tp": None, "rr": None, "trade_geometry_ok": False, "target_path_ok": bool(path.get("ok")), "target_path_structural": bool(path.get("structural")), "stop_source": stop_source, "sl_atr": sl_atr, "tp_distance_atr": 0.0, "target_levels": path.get("target_levels", []), "target_timeframe": path.get("target_timeframe"), "blocking_level": path.get("blocking_level"), "target_path_reason": path.get("reason"), "geometry_reason": str(path.get("reason")), "entry_mode": entry_mode}

    reward = abs(tp - entry)
    rr_gross = reward / abs(entry - stop) if abs(entry - stop) > 0 else 0.0
    cost_pct = max(0.0, _num(data.get("estimated_round_trip_cost_pct"), 0.0015))
    cost = entry * cost_pct
    net_rr = max(0.0, reward - cost) / (abs(entry - stop) + cost) if abs(entry - stop) > 0 else 0.0
    tp_atr = reward / atr_4h
    geometry_ok = bool(side == "LONG" and stop < entry < tp or side == "SHORT" and tp < entry < stop)
    geometry_ok = geometry_ok and MIN_SL_ATR <= sl_atr <= MAX_SL_ATR and tp_atr >= MIN_TP_ATR and net_rr >= MIN_RR and bool(path.get("structural"))
    return {
        "entry": entry, "stop_loss": stop, "tp": tp, "rr": net_rr, "rr_gross": rr_gross,
        "estimated_round_trip_cost_pct": cost_pct, "target_path_ok": bool(path.get("ok")),
        "target_path_structural": bool(path.get("structural")), "target_path_reason": path.get("reason"),
        "target_levels": path.get("target_levels", []), "target_timeframe": path.get("target_timeframe"),
        "blocking_level": path.get("blocking_level"), "stop_source": stop_source, "sl_atr": sl_atr,
        "stop_distance_pct": abs(entry - stop) / entry, "tp_distance_atr": tp_atr,
        "tp_distance_pct": reward / entry, "trade_geometry_ok": geometry_ok,
        "geometry_reason": "OK" if geometry_ok else "trade geometry failed", "entry_mode": entry_mode,
        "entry_limit_price": data.get("limit_price"), "limit_entry_expiry_bars": LIMIT_ENTRY_EXPIRY_BARS,
        "zone_floor": zone_floor, "zone_ceiling": zone_ceiling,
    }

def _direction_aligned(side: str, daily: dict[str, Any], bias: dict[str, Any], primary_structure: str, bos: Optional[dict[str, Any]] = None) -> bool:
    if side == "LONG":
        return not bool(daily.get("bear")) and primary_structure != "LH/LL"
    if side == "SHORT":
        return not bool(daily.get("bull")) and primary_structure != "HH/HL"
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
    twelve_h_context: float = 0.0,
    retest_quality: float = 0.0,
    entry_efficiency: float = 0.0,
    momentum_quality: float = 0.0,
    volume_quality: float = 0.0,
    volatility_quality: float = 0.0,
    # Backward-compatible aliases for older tests/integrations.
    structure_quality: float | None = None,
    trigger_quality: float | None = None,
    location_quality: float | None = None,
    risk_quality: float | None = None,
) -> tuple[int, dict[str, int]]:
    if structure_quality is not None and retest_quality == 0.0:
        retest_quality = structure_quality
    if trigger_quality is not None and entry_efficiency == 0.0:
        entry_efficiency = trigger_quality
    if location_quality is not None and twelve_h_context == 0.0:
        twelve_h_context = location_quality
    groups = {
        "12h_context": int(round(20 * _clamp(twelve_h_context, 0, 1))),
        "retest_absorption": int(round(25 * _clamp(retest_quality, 0, 1))),
        "entry_efficiency": int(round(25 * _clamp(entry_efficiency, 0, 1))),
        "momentum": int(round(10 * _clamp(momentum_quality, 0, 1))),
        "volume": int(round(10 * _clamp(volume_quality, 0, 1))),
        "volatility": int(round(10 * _clamp(volatility_quality, 0, 1))),
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


def build_btc_context(candles_1d: list, candles_12h: list | None, candles_4h: list, candles_1h: list | None = None) -> dict[str, Any]:
    try:
        daily = _macro_regime(convert_candles(candles_1d))
        c4 = convert_candles(candles_4h)
        a4 = _safe_atr(c4)
        structure_4h = _structure_from_swings(*_swing_points(c4))
        e50_4h = _safe_ema([float(c["close"]) for c in c4], 50)
        last_close = float(c4[-1]["close"]) if c4 else 0.0
        bull_4h = bool(e50_4h is not None and last_close > e50_4h and structure_4h != "LH/LL")
        bear_4h = bool(e50_4h is not None and last_close < e50_4h and structure_4h != "HH/HL")
        move4 = (last_close - float(c4[-2]["close"])) / a4 if len(c4) >= 2 and a4 > 0 else 0.0
        move1 = 0.0
        c1 = convert_candles(candles_1h or [])
        if len(c1) >= 2:
            a1 = _safe_atr(c1)
            move1 = (float(c1[-1]["close"]) - float(c1[-2]["close"])) / a1 if a1 > 0 else 0.0
        return {
            "ok": True, "bull_1d": bool(daily.get("bull")), "bear_1d": bool(daily.get("bear")),
            "bull_4h": bull_4h, "bear_4h": bear_4h, "structure_4h": structure_4h,
            "move_4h_atr": move4, "move_1h_atr": move1,
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

def calculate_confluence(data: dict[str, Any]) -> dict[str, Any]:
    return dict(data)


def _diagnostic_failures(data: dict[str, Any]) -> list[str]:
    checks = [
        (not data.get("direction_ok"), "1D/4H direction"),
        (not data.get("structure_ok"), "4H BOS/departure/retest structure"),
        (not data.get("setup_ok"), "4H setup side"),
        (not data.get("confirmation_ok"), "1H confirmation"),
        (not data.get("entry_distance_ok"), "1H entry distance"),
        (not data.get("target_path_ok"), "HTF target path"),
        (not data.get("risk_ok"), "structural risk/RR"),
        (not data.get("shock_veto_ok"), "shock veto"),
        (not data.get("btc_filter_ok"), "BTC regime"),
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
    btc_context: Optional[dict[str, Any]] = None,
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
    if long_candidate and not short_candidate:
        side = "LONG"
    elif short_candidate and not long_candidate:
        side = "SHORT"
    elif long_candidate and short_candidate:
        side = "LONG" if _num(bos_long.get("strength")) >= _num(bos_short.get("strength")) else "SHORT"
    else:
        side = "NONE"

    active_bos = bos_long if side == "LONG" else bos_short if side == "SHORT" else None
    active_retest = dict(ret_long if side == "LONG" else ret_short if side == "SHORT" else {})
    setup = side if side in {"LONG", "SHORT"} else "NO TRADE"
    price = float(c1[-1]["close"])
    atr1 = _safe_atr(c1)
    atr4 = _safe_atr(c4)

    direction_ok = _direction_aligned(side, daily, bias, primary_structure, active_bos)
    structure_ok = bool(active_bos and active_retest.get("valid") and _num(active_bos.get("strength")) >= 0.50)
    setup_ok = side in {"LONG", "SHORT"}
    trigger = _one_hour_trigger_confirmation(c1, side, active_retest) if setup_ok else {"ready": False, "quality": 0.0, "rsi": 50.0, "rvol": 0.0, "atr": atr1, "body_ratio": 0.0, "close_location": 0.0, "candle_time": int(c1[-1]["time"]), "trigger_type": "NONE", "reason": "no active setup", "bars_after_retest": None, "entry_price": None, "entry_mode": "MARKET", "limit_price": None}
    confirmation_ok = bool(trigger.get("ready"))

    if trigger.get("retest_low_1h") is not None:
        active_retest["low"] = trigger.get("retest_low_1h")
    if trigger.get("retest_high_1h") is not None:
        active_retest["high"] = trigger.get("retest_high_1h")

    rsi1 = _safe_rsi([float(c["close"]) for c in c1])
    rvol1 = _relative_volume(c1)
    macd_line, macd_signal, macd_hist, macd_delta = _macd_components([float(c["close"]) for c in c1])
    momentum_quality = _clamp(((rsi1 - 50.0) / 20.0) if setup == "LONG" else ((50.0 - rsi1) / 20.0) if setup == "SHORT" else 0.0, 0, 1)
    if (setup == "LONG" and macd_hist > 0) or (setup == "SHORT" and macd_hist < 0):
        momentum_quality = _clamp(momentum_quality + 0.20, 0, 1)
    atr_rank = _atr_percentile(c1)
    volatility_ok = bool(atr1 > 0 and atr_rank <= MAX_ATR_PERCENTILE)

    frames = [("1D", c1d), ("12H", c12), ("4H", c4), ("1H", c1)]
    levels = calculate_trade_levels({
        "setup": setup, "price": price,
        "entry_price": trigger.get("entry_price") if trigger.get("entry_price") is not None else price,
        "entry_mode": trigger.get("entry_mode", "MARKET"), "limit_price": trigger.get("limit_price"),
        "atr_1h": atr1, "atr_4h": atr4, "retest": active_retest,
        "target_frames": frames, "estimated_round_trip_cost_pct": 0.0015,
    })

    zone_edge = _num(trigger.get("zone_ceiling")) if side == "LONG" else _num(trigger.get("zone_floor")) if side == "SHORT" else 0.0
    entry_distance_atr = abs(price - zone_edge) / atr4 if zone_edge > 0 and atr4 > 0 else float("inf")
    entry_distance_ok = bool(
        trigger.get("entry_mode") == "LIMIT" and entry_distance_atr <= MAX_LIMIT_ENTRY_DISTANCE_ATR
        or trigger.get("entry_mode") == "MARKET" and entry_distance_atr <= MAX_ENTRY_DISTANCE_ATR
    )
    target_path_ok = bool(levels.get("target_path_ok") and levels.get("target_path_structural"))
    location_ok = bool(target_path_ok)
    risk_ok = bool(levels.get("trade_geometry_ok") and _num(levels.get("rr")) >= MIN_RR)
    shock_ok, shock_reason = _shock_veto(c1, setup, atr1) if setup in {"LONG", "SHORT"} else (True, "No active setup")

    # Supporting score: no duplicated hard-gate inputs.
    twelve_h_context = 1.0 if bias.get("structure") == ("HH/HL" if side == "LONG" else "LH/LL") else 0.60 if bias.get("structure") == "RANGE" else 0.30 if setup in {"LONG", "SHORT"} else 0.0
    retest_quality = _clamp(0.65 * _num(active_retest.get("quality")) + 0.35 * _num(trigger.get("quality")), 0, 1)
    entry_efficiency = _clamp(1.0 - entry_distance_atr / max(MAX_LIMIT_ENTRY_DISTANCE_ATR, 1e-9), 0, 1) if math.isfinite(entry_distance_atr) else 0.0
    volatility_quality = 0.95 if 25 <= atr_rank <= 85 else 0.70 if 5 <= atr_rank <= 98 else 0.30
    volume_quality = _clamp(0.65 * _clamp(rvol1 / 1.5, 0, 1) + 0.35 * (1.0 if volume_status(c1) == "INCREASING" else 0.55 if volume_status(c1) == "NORMAL" else 0.25), 0, 1)
    score, score_groups = _build_score(
        twelve_h_context=twelve_h_context, retest_quality=retest_quality,
        entry_efficiency=entry_efficiency, momentum_quality=momentum_quality,
        volume_quality=volume_quality, volatility_quality=volatility_quality,
    )

    btc_ok, btc_reason = btc_filter_ok(side, btc_context or {"ok": False}, is_btc=symbol.upper().startswith("BTC")) if setup_ok else (True, "No active setup")
    technical_candidate = bool(
        setup_ok and direction_ok and structure_ok and confirmation_ok and entry_distance_ok
        and location_ok and risk_ok and shock_ok and btc_ok and score >= MIN_SCORE
    )

    family_result = evaluate_confirmation_families({
        "setup": setup, "momentum_quality": momentum_quality, "rvol_1h": rvol1,
        "atr_percentile": atr_rank, "target_path_structural": levels.get("target_path_structural"),
        "rolling_vwap_12h": _rolling_vwap(c12), "price": price,
        "structure_quality": retest_quality, "trigger_quality": trigger.get("quality"),
    })

    stage_status = {
        "1D_REGIME": bool(daily.get("bull") or daily.get("bear") or daily.get("regime") == "SIDEWAYS"),
        "12H_BIAS": bias.get("structure") in {"HH/HL", "LH/LL", "RANGE"},
        "4H_SETUP": bool(active_bos and active_retest.get("valid")),
        "1H_TRIGGER": confirmation_ok,
        "ENTRY_DISTANCE": entry_distance_ok,
        "TARGET_PATH": target_path_ok,
        "RISK": risk_ok,
        "RR": _num(levels.get("rr")) >= MIN_RR,
        "QUALITY": score >= MIN_SCORE,
        "BTC": btc_ok,
        "SHOCK": shock_ok,
    }
    stage_failures = {
        "1D_REGIME": [] if stage_status["1D_REGIME"] else [str(daily.get("regime") or "unknown")],
        "12H_BIAS": [] if stage_status["12H_BIAS"] else ["no 12H structural context"],
        "4H_SETUP": [] if stage_status["4H_SETUP"] else ["no confirmed 4H BOS/departure/retest"],
        "1H_TRIGGER": [] if confirmation_ok else [str(trigger.get("reason") or "no 1H retest rejection")],
        "ENTRY_DISTANCE": [] if entry_distance_ok else [f"entry distance {entry_distance_atr:.2f} ATR4H exceeds execution limit"],
        "TARGET_PATH": [] if target_path_ok else [str(levels.get("target_path_reason") or "no nearest HTF target")],
        "RISK": [] if risk_ok else [str(levels.get("geometry_reason") or "risk geometry failed")],
        "RR": [] if stage_status["RR"] else [f"RR {_num(levels.get('rr')):.2f} < {MIN_RR:.2f}"],
        "QUALITY": [] if stage_status["QUALITY"] else [f"score {score} < {MIN_SCORE}"],
        "BTC": [] if btc_ok else [btc_reason],
        "SHOCK": [] if shock_ok else [shock_reason],
    }
    failures = _diagnostic_failures({
        "direction_ok": direction_ok, "structure_ok": structure_ok, "setup_ok": setup_ok,
        "confirmation_ok": confirmation_ok, "volatility_ok": volatility_ok,
        "target_path_ok": target_path_ok, "entry_distance_ok": entry_distance_ok,
        "risk_ok": risk_ok, "shock_veto_ok": shock_ok, "score": score,
        "btc_filter_ok": btc_ok,
    })

    retest_out = dict(active_retest)
    retest_out.update({
        "zone_floor": trigger.get("zone_floor", retest_out.get("zone_floor")),
        "zone_ceiling": trigger.get("zone_ceiling", retest_out.get("zone_ceiling")),
        "retest_low_1h": trigger.get("retest_low_1h"), "retest_high_1h": trigger.get("retest_high_1h"),
        "state": "CONFIRMED" if confirmation_ok else "RETEST_TOUCHED",
    })
    reasons = []
    if daily.get("regime") != "SIDEWAYS": reasons.append(f"1D macro regime: {daily.get('regime')}")
    reasons.append(f"12H context: {bias.get('structure')}")
    if structure_ok: reasons.append(f"4H {side} BOS + departure + retest confirmed")
    if confirmation_ok: reasons.append("1H retest rejection/reclaim")
    if location_ok: reasons.append("Nearest HTF target available")
    if risk_ok: reasons.append(f"Post-cost RR {float(levels.get('rr') or 0):.2f}")
    if not technical_candidate: reasons.append("Technical candidate gate failed")

    return {
        "symbol": symbol.upper(), "price": price, "setup": setup, "setup_candidate": side,
        "regime_1d": daily.get("regime"), "trend_4h": primary_structure,
        "bias_12h": "BULLISH" if bias.get("bull") else "BEARISH" if bias.get("bear") else "NEUTRAL",
        "daily_structure_1d": daily.get("structure"), "structure_12h": bias.get("structure"), "structure_4h": primary_structure,
        "protected_structure_4h": protected4.get("state"), "protected_high": protected4.get("protected_high"), "protected_low": protected4.get("protected_low"),
        "bos_4h": bool(active_bos), "bos_4h_time": active_bos.get("time") if active_bos else None,
        "bos_4h_strength": _num((active_bos or {}).get("strength")), "long_bos_level": bos_long.get("level") if bos_long else None,
        "short_bos_level": bos_short.get("level") if bos_short else None, "long_retest": bool(ret_long.get("valid")),
        "short_retest": bool(ret_short.get("valid")), "long_retest_time": ret_long.get("time"), "short_retest_time": ret_short.get("time"),
        "retest": retest_out,
        "ema21_1d": daily.get("e21"), "ema50_1d": daily.get("e50"), "ema200_1d": daily.get("e200"),
        "ema21_12h": bias.get("e21"), "ema50_12h": bias.get("e50"),
        "ema21_4h": _safe_ema([float(c["close"]) for c in c4], 21), "ema50_4h": _safe_ema([float(c["close"]) for c in c4], 50),
        "ema21_1h": _safe_ema([float(c["close"]) for c in c1], 21), "ema50_1h": _safe_ema([float(c["close"]) for c in c1], 50),
        "ema_direction": "BULLISH" if _num(_safe_ema([float(c["close"]) for c in c1], 21)) >= _num(_safe_ema([float(c["close"]) for c in c1], 50)) else "BEARISH",
        "rsi": rsi1, "rsi_1h_entry": trigger.get("rsi", rsi1), "macd": macd_line, "macd_signal": macd_signal,
        "macd_hist": macd_hist, "macd_hist_delta": macd_delta, "atr": atr1, "atr_1h": atr1, "atr_4h": atr4,
        "atr_pct": atr1 / price if price > 0 else 0.0, "atr_percentile": atr_rank, "adx_1d": daily.get("adx"),
        "ema50_slope_1d": daily.get("slope"), "volume": volume_status(c1), "rvol": rvol1, "rvol_1h": rvol1,
        "support": max([x["price"] for x in _collect_structural_levels(frames, price) if x["price"] < price], default=None),
        "resistance": min([x["price"] for x in _collect_structural_levels(frames, price) if x["price"] > price], default=None),
        "futures_context": "PENDING", "futures_ok": True, "btc_filter_ok": btc_ok, "btc_filter_reason": btc_reason, "btc_context": btc_context or {"ok": False},
        "data_fresh": True, "signal_engine_version": ENGINE_VERSION,
        "signal_basis": "1D macro + 12H context + 4H BOS/departure/retest + 1H rejection + structural SL/nearest HTF TP",
        "primary_entry_timeframe": "1H", "setup_timeframe": "4H", "signal_candle_timeframe": "1H",
        "intraday_max_hold_minutes": DEFAULT_MAX_HOLD_MINUTES, "trigger_side": side,
        "trigger_quality": trigger.get("quality", 0.0), "trigger_type": trigger.get("trigger_type", "NONE"),
        "trigger_reason": trigger.get("reason", ""), "trigger_close_location": trigger.get("close_location", 0.0),
        "structure_quality_ok": structure_ok, "momentum_quality": momentum_quality, "volume_quality": volume_quality,
        "volatility_quality": volatility_quality, "entry_efficiency": entry_efficiency, "twelve_h_context_quality": twelve_h_context,
        "entry_mode": trigger.get("entry_mode", "MARKET"), "limit_price": trigger.get("limit_price"),
        "limit_entry_expiry_bars": LIMIT_ENTRY_EXPIRY_BARS, "entry_1h_ready": confirmation_ok,
        "shock_veto_ok": shock_ok, "shock_veto_reason": shock_reason, "score": score, "score_groups": score_groups,
        "confirmation_families": family_result.get("families", {}), "confirmation_families_passed": int(family_result.get("passed", 0)),
        "confirmation_families_available": int(family_result.get("available", 0)), "confirmation_family_diversity_ok": bool(family_result.get("diversity_ok")),
        "direction_ok": direction_ok, "structure_ok": structure_ok, "setup_ok": setup_ok, "confirmation_ok": confirmation_ok,
        "momentum_ok": momentum_quality >= 0.45, "volume_ok": volume_quality >= 0.50, "location_ok": location_ok,
        "volatility_ok": volatility_ok, "risk_ok": risk_ok, "entry_distance_ok": entry_distance_ok,
        "stage_status": stage_status, "stage_failures": stage_failures, "technical_candidate": technical_candidate,
        "signal_blocked": not technical_candidate,
        "rejection_stage": next((k for k,v in [("DIRECTION",direction_ok),("4H_SETUP",structure_ok and setup_ok),("1H_TRIGGER",confirmation_ok),("ENTRY_DISTANCE",entry_distance_ok),("TARGET_PATH",target_path_ok),("RISK",risk_ok),("BTC",btc_ok),("QUALITY",score>=MIN_SCORE)] if not v), None),
        "technical_gate_failures": failures, "diagnostic_failures": failures, "reasons": reasons,
        "entry": levels.get("entry"), "stop_loss": levels.get("stop_loss"), "tp": levels.get("tp"), "rr": levels.get("rr"), "rr_gross": levels.get("rr_gross"),
        "sl_atr": levels.get("sl_atr", 0.0), "stop_distance_pct": levels.get("stop_distance_pct", 0.0), "tp_distance_atr": levels.get("tp_distance_atr", 0.0),
        "tp_distance_pct": levels.get("tp_distance_pct", 0.0), "target_path_ok": levels.get("target_path_ok", False),
        "target_path_structural": levels.get("target_path_structural", False), "target_path_reason": levels.get("target_path_reason"),
        "target_timeframe": levels.get("target_timeframe"), "target_levels": levels.get("target_levels", []), "blocking_level": levels.get("blocking_level"),
        "entry_distance_atr": entry_distance_atr, "entry_distance_ok": entry_distance_ok, "entry_price_reference": price,
        "stop_source": levels.get("stop_source"), "trade_geometry_ok": levels.get("trade_geometry_ok", False), "geometry_reason": levels.get("geometry_reason"),
        "entry_limit_price": levels.get("entry_limit_price"), "candle_open_time": int(c1[-1]["time"]), "candle_close_time": int(c1[-1]["time"])+TIMEFRAME_MS["1h"],
        "candle_time": int(c1[-1]["time"])+TIMEFRAME_MS["1h"], "trigger_candle_open_time": trigger.get("candle_time"),
        "setup_bos_time": active_bos.get("time") if active_bos else None, "setup_retest_time": active_retest.get("time") if active_retest else None,
        "rolling_vwap_12h": _rolling_vwap(c12), "flow_proxy_ratio": _backtest_flow_proxy(c1),
        "closed_1d_candles": len(c1d), "closed_12h_candles": len(c12), "closed_4h_candles": len(c4), "closed_1h_candles": len(c1),
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
