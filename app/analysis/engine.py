from __future__ import annotations

"""Deterministic MEXC Futures multi-timeframe signal engine.

Decision hierarchy:
1D context -> 4H regime -> 1H direction/protected structure ->
15M BOS + post-BOS retest -> 5M trigger -> momentum/volume ->
structural target path -> structural stop -> hard technical gates -> grouped score.

This module never places an order and never fabricates futures-market context.
The scanner adds live MEXC quote/order-book/trade/funding/BTC context before
final validation.
"""

from dataclasses import dataclass
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .indicators import atr, ema, rsi, volume_status
from .structure import get_structure, get_support_resistance

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
MIN_ATR_PERCENTILE = 20.0
MAX_ATR_PERCENTILE = 95.0
MAX_SETUP_AGE_15M = 8
MAX_ENTRY_DISTANCE_ATR = 1.20
BOS_BUFFER_ATR = 0.10
BOS_BUFFER_PCT = 0.0005
BTC_SHOCK_ATR = 1.75
MAX_SIGNAL_AGE_SECONDS = 330
ENGINE_VERSION = "gold-v1.3-deterministic"


class Candle(dict):
    """Normalized candle supporting both dict and legacy positional access."""

    _legacy_keys = ("time", "open", "high", "low", "close", "volume")

    def __getitem__(self, key):
        if isinstance(key, int):
            if 0 <= key < len(self._legacy_keys):
                key = self._legacy_keys[key]
        return super().__getitem__(key)


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
            if not all(math.isfinite(float(candle[k])) for k in ("open", "high", "low", "close", "volume")):
                continue
            if candle["open"] <= 0 or candle["high"] <= 0 or candle["low"] <= 0 or candle["close"] <= 0:
                continue
            if candle["low"] > candle["high"]:
                continue
            out.append(candle)
        except (TypeError, ValueError, OverflowError):
            continue
    out.sort(key=lambda x: int(x["time"]))
    # Keep only the latest observation when a duplicate timestamp appears.
    dedup: dict[int, Candle] = {}
    for candle in out:
        dedup[int(candle["time"])] = candle
    return [dedup[t] for t in sorted(dedup)]


def closed_candle_rows(
    candles: Iterable[Any] | None,
    timeframe_ms: Any,
    now_ms: Optional[int] = None,
) -> List[Candle]:
    """Normalize MEXC candles and remove the currently-forming candle."""
    interval = _timeframe_ms(timeframe_ms)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    out = convert_candles(candles)
    return [c for c in out if int(c["time"]) + interval <= now]


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
    return (series[-1] - series[-lookback - 1]) / base if base else 0.0


def _safe_rsi(values: List[float], period: int = 14) -> float:
    try:
        return _num(rsi(values, period), 50.0)
    except Exception:
        return 50.0


def _true_ranges(candles: List[Candle]) -> List[float]:
    if not candles:
        return []
    tr: list[float] = []
    previous = float(candles[0]["close"])
    tr.append(float(candles[0]["high"]) - float(candles[0]["low"]))
    for candle in candles[1:]:
        h = float(candle["high"])
        l = float(candle["low"])
        tr.append(max(h - l, abs(h - previous), abs(l - previous)))
        previous = float(candle["close"])
    return tr


def _safe_atr(candles: List[Candle], period: int = 14) -> float:
    try:
        value = atr(candles, period)
        return max(0.0, _num(value))
    except Exception:
        return 0.0


def _atr_series(candles: List[Candle], period: int = 14) -> List[float]:
    trs = _true_ranges(candles)
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
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        up = float(cur["high"]) - float(prev["high"])
        down = float(prev["low"]) - float(cur["low"])
        trs.append(max(
            float(cur["high"]) - float(cur["low"]),
            abs(float(cur["high"]) - float(prev["close"])),
            abs(float(cur["low"]) - float(prev["close"])),
        ))
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
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
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
    average = sum(float(c["volume"]) for c in candles[-lookback - 1:-1]) / lookback
    return current / average if average > 0 else 0.0


def _atr_percentile(candles: List[Candle], period: int = 14, lookback: int = 100) -> float:
    atr_values = _atr_series(candles, period)
    if len(candles) < period + lookback + 1:
        return 50.0
    ratios: list[float] = []
    start = max(period, len(candles) - lookback)
    for i in range(start, len(candles)):
        price = float(candles[i]["close"])
        a = atr_values[i]
        if price > 0 and a > 0:
            ratios.append(a / price)
    if not ratios:
        return 50.0
    current = ratios[-1]
    return 100.0 * sum(v <= current for v in ratios) / len(ratios)


def _swing_highs(candles: List[Candle], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    result: list[Tuple[int, float]] = []
    for i in range(left, len(candles) - right):
        high = float(candles[i]["high"])
        if all(high > float(candles[j]["high"]) for j in range(i - left, i)) and all(high > float(candles[j]["high"]) for j in range(i + 1, i + right + 1)):
            result.append((i, high))
    return result


def _swing_lows(candles: List[Candle], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    result: list[Tuple[int, float]] = []
    for i in range(left, len(candles) - right):
        low = float(candles[i]["low"])
        if all(low < float(candles[j]["low"]) for j in range(i - left, i)) and all(low < float(candles[j]["low"]) for j in range(i + 1, i + right + 1)):
            result.append((i, low))
    return result


def _protected_structure(candles: List[Candle]) -> Dict[str, Any]:
    highs = _swing_highs(candles)
    lows = _swing_lows(candles)
    ph = highs[-1][1] if highs else None
    pl = lows[-1][1] if lows else None
    if len(highs) < 2 or len(lows) < 2:
        return {"state": "NEUTRAL", "protected_high": ph, "protected_low": pl}
    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]
    if h2 > h1 and l2 > l1:
        state = "BULLISH"
    elif h2 < h1 and l2 < l1:
        state = "BEARISH"
    else:
        state = "NEUTRAL"
    return {"state": state, "protected_high": ph, "protected_low": pl}


def _bos_strength(candle: Candle, level: float, atr_value: float) -> float:
    rng = max(float(candle["high"]) - float(candle["low"]), 1e-12)
    body_ratio = abs(float(candle["close"]) - float(candle["open"])) / rng
    displacement = abs(float(candle["close"]) - level) / atr_value if atr_value > 0 else 0.0
    return min(1.0, 0.5 * min(body_ratio / 0.55, 1.0) + 0.5 * min(displacement / 0.5, 1.0))


def _bos_events(candles: List[Candle], side: str, lookback: int = 60) -> List[Dict[str, Any]]:
    """Return confirmed, non-lookahead BOS events in the recent window.

    A BOS may break an older confirmed pivot while a newer pivot remains
    unbroken. The previous implementation only inspected the newest pivot,
    which could hide valid structural breaks and produce zero setups.
    """
    if len(candles) < 10 or side not in {"LONG", "SHORT"}:
        return []

    atr_values = _atr_series(candles, 14)
    highs = _swing_highs(candles)
    lows = _swing_lows(candles)
    pivots = highs if side == "LONG" else lows
    events: list[Dict[str, Any]] = []
    start = max(1, len(candles) - lookback)

    for i in range(start, len(candles)):
        a = atr_values[i]
        if a <= 0:
            continue

        close = float(candles[i]["close"])
        prev_close = float(candles[i - 1]["close"])
        buffer = max(a * BOS_BUFFER_ATR, close * BOS_BUFFER_PCT)

        # Check the newest confirmed pivot first, but fall back to older
        # confirmed pivots on the same candle. This preserves chronology
        # while avoiding the "latest pivot only" blind spot.
        candidates = [
            (idx, price)
            for idx, price in pivots
            if idx + 2 <= i
        ]
        for swing_idx, level in reversed(candidates):
            level = float(level)
            if side == "LONG":
                crossed = prev_close <= level + buffer and close > level + buffer
            else:
                crossed = prev_close >= level - buffer and close < level - buffer

            if not crossed:
                continue

            events.append({
                "index": i,
                "time": int(candles[i]["time"]),
                "level": level,
                "atr": a,
                "strength": _bos_strength(candles[i], level, a),
                "swing_index": swing_idx,
            })
            # One deterministic BOS event per candle/side.
            break

    return events


def _pullback_retest(candles: List[Candle], side: str, bos: Optional[Dict[str, Any]], max_bars: int = 8) -> Dict[str, Any]:
    """Find a post-BOS retest using a bounded structural zone.

    The retest must occur strictly after the BOS candle. The candle range
    only needs to intersect the BOS zone; requiring the exact low/high to sit
    inside a narrow band was too brittle for volatile MEXC futures candles.
    """
    invalid = {"valid": False, "index": None, "time": None, "level": bos.get("level") if bos else None, "quality": 0.0, "rejection": False, "low": None, "high": None}
    if not bos:
        return invalid

    start = int(bos["index"]) + 1
    end = min(len(candles), start + max_bars)
    if start >= end:
        return invalid

    level = float(bos["level"])
    atr_value = max(_num(bos.get("atr")), 0.0)
    if atr_value <= 0:
        return invalid

    # Bounded retest zone: tolerant enough for normal futures noise, but not
    # wide enough to become an arbitrary pullback.
    tolerance = max(atr_value * 0.35, abs(level) * 0.0015)
    penetration = max(atr_value * 0.75, abs(level) * 0.0030)
    close_tolerance = max(atr_value * 0.15, abs(level) * 0.00075)

    for i in range(start, end):
        c = candles[i]
        open_price = float(c["open"])
        low = float(c["low"])
        high = float(c["high"])
        close = float(c["close"])
        rng = max(high - low, 1e-12)

        if side == "LONG":
            intersects = low <= level + tolerance and high >= level - penetration
            held = close >= level - close_tolerance
            wick = min(open_price, close) - low
        else:
            intersects = high >= level - tolerance and low <= level + penetration
            held = close <= level + close_tolerance
            wick = high - max(open_price, close)

        rejection = intersects and held and wick / rng >= 0.20
        if intersects and held:
            # More quality when the candle actually rejects from the zone.
            quality = 0.70 + (0.30 if rejection else 0.0)
            return {
                "valid": True,
                "index": i,
                "time": int(c["time"]),
                "level": level,
                "quality": quality,
                "rejection": bool(rejection),
                "low": low,
                "high": high,
            }

    return invalid


def _select_latest_bos_with_retest(candles: List[Candle], side: str) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    events = _bos_events(candles, side)
    latest_index = len(candles) - 1
    for bos in reversed(events):
        # Setup freshness is measured from the active BOS to the current
        # closed 15M candle. Old structure is not allowed to become a new signal.
        if latest_index - int(bos["index"]) > MAX_SETUP_AGE_15M + 2:
            continue
        retest = _pullback_retest(candles, side, bos, max_bars=MAX_SETUP_AGE_15M)
        if retest["valid"] and latest_index - int(retest["index"]) <= MAX_SETUP_AGE_15M:
            return bos, retest
    return None, {"valid": False, "index": None, "time": None, "level": None, "quality": 0.0, "rejection": False, "low": None, "high": None}


def _five_minute_trigger(candles: List[Candle], side: str, setup_level: Optional[float]) -> Dict[str, Any]:
    empty = {"ready": False, "long": False, "short": False, "quality": 0.0, "rsi": 50.0, "rvol": 0.0, "atr": 0.0, "candle_time": 0, "body_ratio": 0.0}
    if len(candles) < 30:
        return empty
    c = candles[-1]; prev = candles[-2]
    close = float(c["close"]); rng = max(float(c["high"]) - float(c["low"]), 1e-12)
    body_ratio = abs(close - float(c["open"])) / rng
    r = _safe_rsi([float(x["close"]) for x in candles])
    rv = _relative_volume(candles)
    a = _safe_atr(candles)
    bullish_body = close > float(c["open"])
    bearish_body = close < float(c["open"])
    long_ok = bullish_body and close > float(prev["high"]) and (setup_level is None or close > setup_level) and r > 50 and rv >= 1.0 and body_ratio >= 0.55
    short_ok = bearish_body and close < float(prev["low"]) and (setup_level is None or close < setup_level) and r < 50 and rv >= 1.0 and body_ratio >= 0.55
    if side == "LONG":
        strength_rsi = max(0.0, min((r - 50.0) / 15.0, 1.0))
    elif side == "SHORT":
        strength_rsi = max(0.0, min((50.0 - r) / 15.0, 1.0))
    else:
        strength_rsi = 0.0
    quality = min(1.0, 0.35 * min(body_ratio / 0.70, 1.0) + 0.35 * min(rv / 1.50, 1.0) + 0.30 * strength_rsi)
    return {
        "ready": bool(long_ok or short_ok),
        "long": bool(long_ok),
        "short": bool(short_ok),
        "quality": quality,
        "rsi": r,
        "rvol": rv,
        "atr": a,
        "candle_time": int(c["time"]),
        "body_ratio": body_ratio,
    }


def _macd(values: List[float]) -> Tuple[float, float, float]:
    fast = _ema_series(values, 12); slow = _ema_series(values, 26)
    if not fast or not slow:
        return 0.0, 0.0, 0.0
    n = min(len(fast), len(slow))
    line_series = [fast[-n + i] - slow[-n + i] for i in range(n)]
    signal_series = _ema_series(line_series, 9)
    line = line_series[-1]
    signal = signal_series[-1] if signal_series else 0.0
    return line, signal, line - signal


def _level_clusters(candles: List[Candle], atr_value: float, lookback: int = 100) -> Tuple[Optional[float], Optional[float]]:
    recent = candles[-lookback:] if len(candles) > lookback else candles
    if not recent:
        return None, None
    current = float(recent[-1]["close"])
    tol = max(atr_value * 0.20, current * 0.001)
    highs = [float(c["high"]) for c in recent if float(c["high"]) > current + tol]
    lows = [float(c["low"]) for c in recent if float(c["low"]) < current - tol]
    return (max(lows) if lows else None, min(highs) if highs else None)


def _collect_structural_levels(
    frames: List[Tuple[str, List[Candle]]],
    atr_value: float,
    entry: float,
    max_swings_per_frame: int = 12,
) -> List[Dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    for timeframe, candles in frames:
        if not candles:
            continue
        for idx, price in _swing_highs(candles)[-max_swings_per_frame:]:
            if price > entry:
                raw.append({"price": float(price), "timeframe": timeframe, "index": idx, "kind": "RESISTANCE"})
        for idx, price in _swing_lows(candles)[-max_swings_per_frame:]:
            if price < entry:
                raw.append({"price": float(price), "timeframe": timeframe, "index": idx, "kind": "SUPPORT"})
    if not raw:
        return []
    tolerance = max(0.10 * atr_value, entry * 0.0005)
    raw.sort(key=lambda x: float(x["price"]))
    clusters: list[dict[str, Any]] = []
    for level in raw:
        if not clusters or abs(float(level["price"]) - float(clusters[-1]["price"])) > tolerance:
            clusters.append(level.copy())
        else:
            # Keep the level at the more conservative/higher-timeframe location.
            priority = {"1D": 4, "4H": 3, "1H": 2, "15M": 1}
            if priority.get(level["timeframe"], 0) > priority.get(clusters[-1]["timeframe"], 0):
                clusters[-1] = level.copy()
    return clusters


def _target_path(
    frames: List[Tuple[str, List[Candle]]],
    side: str,
    entry: float,
    stop: float,
    atr_value: float,
) -> Dict[str, Any]:
    risk = abs(entry - stop)
    if risk <= 0 or atr_value <= 0:
        return {
            "ok": False,
            "tp1": None,
            "tp2": None,
            "obstacle": None,
            "reason": "zero risk or ATR",
            "risk": risk,
            "structural": False,
        }

    levels = _collect_structural_levels(frames, atr_value, entry)
    clearance = 0.10 * atr_value
    min_tp1 = 1.20 * risk
    min_tp2 = 2.00 * risk

    if side == "LONG":
        ordered = [x for x in levels if float(x["price"]) > entry + clearance]
        ordered.sort(key=lambda x: float(x["price"]))
    elif side == "SHORT":
        ordered = [x for x in levels if float(x["price"]) < entry - clearance]
        ordered.sort(key=lambda x: float(x["price"]), reverse=True)
    else:
        return {
            "ok": False,
            "tp1": None,
            "tp2": None,
            "obstacle": None,
            "reason": "invalid side",
            "risk": risk,
            "structural": False,
        }

    if not ordered:
        target_kind = "resistance" if side == "LONG" else "support"
        return {
            "ok": False,
            "tp1": None,
            "tp2": None,
            "obstacle": None,
            "reason": f"no confirmed {target_kind} target",
            "risk": risk,
            "structural": False,
        }

    tp1 = None
    tp1_level = None
    for level in ordered:
        distance = (float(level["price"]) - entry) if side == "LONG" else (entry - float(level["price"]))
        if distance >= min_tp1:
            tp1 = float(level["price"])
            tp1_level = level
            break

    if tp1 is None:
        return {
            "ok": False,
            "tp1": float(ordered[0]["price"]),
            "tp2": None,
            "obstacle": float(ordered[0]["price"]),
            "reason": "nearest structural target is closer than 1.20R",
            "risk": risk,
            "structural": True,
            "target_levels": ordered[:6],
        }

    # TP2 must be a different, farther structural/liquidity level.
    tp2 = None
    tp2_level = None
    for level in ordered:
        price = float(level["price"])
        if side == "LONG":
            distance = price - entry
            farther = price > tp1 + clearance
        else:
            distance = entry - price
            farther = price < tp1 - clearance
        if farther and distance >= min_tp2:
            tp2 = price
            tp2_level = level
            break

    if tp2 is None:
        return {
            "ok": False,
            "tp1": tp1,
            "tp2": None,
            "obstacle": None,
            "reason": "no second structural target reaches 2.00R",
            "risk": risk,
            "structural": True,
            "tp1_level": tp1_level,
            "target_levels": ordered[:6],
        }

    return {
        "ok": True,
        "tp1": tp1,
        "tp2": tp2,
        "obstacle": None,
        "reason": "two distinct multi-timeframe structural targets",
        "risk": risk,
        "structural": True,
        "tp1_level": tp1_level,
        "tp2_level": tp2_level,
        "target_levels": ordered[:6],
    }


def calculate_trade_levels(data: Dict[str, Any]) -> Dict[str, Any]:
    side = str(data.get("setup") or "").upper()
    entry = _num(data.get("price"))
    atr15 = _num(data.get("atr"))
    if side not in {"LONG", "SHORT"} or entry <= 0 or atr15 <= 0:
        return {"entry": None, "stop_loss": None, "tp1": None, "tp2": None, "rr": None, "target_path_ok": False}

    retest = data.get("retest") or {}
    if side == "LONG":
        anchors = [retest.get("low"), data.get("protected_low"), data.get("support")]
        candidates = [float(x) for x in anchors if x is not None and _num(x) < entry]
        anchor = float(retest["low"]) if retest.get("low") is not None and _num(retest.get("low")) < entry else (max(candidates) if candidates else entry - atr15)
        stop = anchor - 0.12 * atr15
    else:
        anchors = [retest.get("high"), data.get("protected_high"), data.get("resistance")]
        candidates = [float(x) for x in anchors if x is not None and _num(x) > entry]
        anchor = float(retest["high"]) if retest.get("high") is not None and _num(retest.get("high")) > entry else (min(candidates) if candidates else entry + atr15)
        stop = anchor + 0.12 * atr15

    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        return {"entry": entry, "stop_loss": None, "tp1": None, "tp2": None, "rr": None, "target_path_ok": False}

    frames = data.get("target_frames") or [("15M", data.get("_candles_15m", []))]
    path = _target_path(frames, side, entry, stop, atr15)
    tp1 = path.get("tp1"); tp2 = path.get("tp2")
    risk = abs(entry - stop)
    rr = abs(tp2 - entry) / risk if tp2 is not None and risk > 0 else None
    return {
        "entry": entry,
        "stop_loss": float(stop),
        "tp1": float(tp1) if tp1 is not None else None,
        "tp2": float(tp2) if tp2 is not None else None,
        "rr": rr,
        "target_path_ok": bool(path.get("ok")),
        "target_path_structural": bool(path.get("structural")),
        "target_obstacle": path.get("obstacle"),
        "target_path_reason": path.get("reason"),
        "target_levels": path.get("target_levels", []),
    }


def _build_score(
    *, direction_ok: bool, structure_ok: bool, setup_ok: bool,
    momentum_ok: bool, volume_ok: bool, location_ok: bool,
    futures_ok: bool, volatility_ok: bool, trigger_quality: float = 0.0,
    rvol: float = 0.0, bos_quality: float = 0.0, retest_quality: float = 0.0,
) -> Tuple[int, Dict[str, int], int]:
    groups = {
        "direction_regime": 20 if direction_ok else 0,
        "market_structure": 20 if structure_ok else 0,
        "setup_entry_trigger": 20 if setup_ok else 0,
        "momentum": 10 if momentum_ok else 0,
        "volume_participation": 10 if volume_ok else 0,
        "location_target_path": 10 if location_ok else 0,
        "futures_market_context": 5 if futures_ok else 0,
        "volatility_execution": 5 if volatility_ok else 0,
    }
    if groups["setup_entry_trigger"]:
        quality = 0.50 * trigger_quality + 0.25 * bos_quality + 0.25 * retest_quality
        if quality < 0.60:
            groups["setup_entry_trigger"] -= 5
    if groups["volume_participation"] and rvol < 1.25:
        groups["volume_participation"] -= 2
    families = sum(bool(x) for x in (direction_ok, structure_ok, setup_ok, momentum_ok, volume_ok, location_ok))
    return max(0, min(100, sum(groups.values()))), groups, families


def _data_quality(candles: List[Candle], timeframe_ms: int, minimum: int) -> Tuple[bool, str]:
    if len(candles) < minimum:
        return False, f"not enough candles ({len(candles)}<{minimum})"
    times = [int(c["time"]) for c in candles[-minimum:]]
    if any(b <= a for a, b in zip(times, times[1:])):
        return False, "non-monotonic timestamps"
    recent_gap = times[-1] - times[-2]
    if recent_gap > timeframe_ms * 2:
        return False, "recent candle gap"
    return True, "OK"


def _four_hour_regime(candles: List[Candle]) -> dict[str, Any]:
    close = [float(c["close"]) for c in candles]
    e21 = _safe_ema(close, 21); e50 = _safe_ema(close, 50); e100 = _safe_ema(close, 100); e200 = _safe_ema(close, 200)
    atr4 = _safe_atr(candles); adx4 = _adx(candles); slope4 = _ema_slope(close, 50)
    bull = bool(e21 is not None and e50 is not None and e100 is not None and e200 is not None and close[-1] > e200 and e21 > e50 > e100 > e200 and slope4 > 0 and adx4 >= 25)
    bear = bool(e21 is not None and e50 is not None and e100 is not None and e200 is not None and close[-1] < e200 and e21 < e50 < e100 < e200 and slope4 < 0 and adx4 >= 25)
    protected = _protected_structure(candles)
    if bull and protected["state"] == "BULLISH":
        regime = "BULLISH"
    elif bear and protected["state"] == "BEARISH":
        regime = "BEARISH"
    else:
        regime = "NO_TRADE"
    return {"bull": bull, "bear": bear, "regime": regime, "e21": e21, "e50": e50, "e100": e100, "e200": e200, "atr": atr4, "adx": adx4, "slope": slope4, "protected": protected}


def build_btc_context(candles_4h: List[Candle], candles_1h: List[Candle], candles_15m: List[Candle]) -> Dict[str, Any]:
    """Build the market-wide BTC gate from the same closed-candle rules."""
    regime = _four_hour_regime(candles_4h)
    structure1 = get_structure(candles_1h)
    protected1 = _protected_structure(candles_1h)
    close15 = [float(c["close"]) for c in candles_15m]
    atr15 = _safe_atr(candles_15m)
    move_atr = ((close15[-1] - close15[-2]) / atr15) if len(close15) >= 2 and atr15 > 0 else 0.0
    strong_bull_1h = structure1 == "HH/HL" and protected1["state"] == "BULLISH"
    strong_bear_1h = structure1 == "LH/LL" and protected1["state"] == "BEARISH"
    return {
        "ok": True,
        "bull_4h": bool(regime["bull"]),
        "bear_4h": bool(regime["bear"]),
        "regime_4h": regime["regime"],
        "structure_1h": structure1,
        "strong_bull_1h": strong_bull_1h,
        "strong_bear_1h": strong_bear_1h,
        "move_15m_atr": move_atr,
        "candle_time_15m": int(candles_15m[-1]["time"]),
    }


def btc_filter_ok(side: str, context: Dict[str, Any], *, is_btc: bool = False) -> tuple[bool, str]:
    if is_btc:
        return True, "BTC self-filter"
    if not context or not context.get("ok"):
        return False, "BTC context unavailable"
    side = side.upper()
    move = _num(context.get("move_15m_atr"))
    if side == "LONG":
        if context.get("bear_4h") or context.get("strong_bear_1h"):
            return False, "BTC is directionally bearish against LONG"
        if move <= -BTC_SHOCK_ATR:
            return False, "BTC 15M shock against LONG"
    elif side == "SHORT":
        if context.get("bull_4h") or context.get("strong_bull_1h"):
            return False, "BTC is directionally bullish against SHORT"
        if move >= BTC_SHOCK_ATR:
            return False, "BTC 15M shock against SHORT"
    else:
        return False, "Invalid side"
    return True, "OK"


def calculate_confluence(data: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy compatibility helper for the original unit tests/manual callers."""
    result = dict(data)
    side = str(result.get("setup") or "").upper()
    trend = str(result.get("trend_4h") or "").upper()
    structure = str(result.get("structure_1h") or "").upper()
    bos_raw = result.get("bos_15m")
    bos = str(bos_raw or "").upper()
    bos_bullish = bool(bos_raw is True or bos_raw == 1 or "BULLISH BOS" in bos)
    bos_bearish = bool((bos_raw is False and bos_raw is not None) or bos_raw == -1 or "BEARISH BOS" in bos)
    ema_direction = str(result.get("ema_direction") or "").upper()
    rsi_value = _num(result.get("rsi"), 50.0)
    score = 0
    if side == "LONG":
        score = sum((trend == "BULLISH", structure == "HH/HL", bos_bullish, ema_direction == "BULLISH", rsi_value > 50))
        if "BEARISH" in trend or structure == "LH/LL" or bos_bearish or ema_direction == "BEARISH":
            result["setup"] = "NO TRADE"
    elif side == "SHORT":
        score = sum((trend == "BEARISH", structure == "LH/LL", bos_bearish, ema_direction == "BEARISH", rsi_value < 50))
        if "BULLISH" in trend or structure == "HH/HL" or bos_bullish or ema_direction == "BULLISH":
            result["setup"] = "NO TRADE"
    volume = str(result.get("volume") or "").upper()
    if volume == "INCREASING":
        score += 1
    result["score"] = int(score)
    return result


def analyze_candles(
    symbol: str,
    candles_4h: List,
    candles_1h: List,
    candles_15m: List,
    candles_5m: Optional[List] = None,
    candles_1d: Optional[List] = None,
) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    c4 = closed_candle_rows(candles_4h, "4h", now_ms)
    c1 = closed_candle_rows(candles_1h, "1h", now_ms)
    c15 = closed_candle_rows(candles_15m, "15m", now_ms)
    c5 = closed_candle_rows(candles_5m or [], "5m", now_ms)
    c1d = closed_candle_rows(candles_1d or [], "1d", now_ms)
    for candles, tf, minimum in ((c4, "4h", 205), (c1, "1h", 205), (c15, "15m", 80), (c5, "5m", 30)):
        ok, reason = _data_quality(candles, TIMEFRAME_MS[tf], minimum)
        if not ok:
            raise ValueError(f"{symbol}: {reason}")

    close4 = [float(c["close"]) for c in c4]; close1 = [float(c["close"]) for c in c1]; close15 = [float(c["close"]) for c in c15]
    price = close15[-1]
    regime4 = _four_hour_regime(c4)
    structure1 = get_structure(c1)
    protected1 = _protected_structure(c1)
    e21_1 = _safe_ema(close1, 21); e50_1 = _safe_ema(close1, 50)
    long1 = regime4["bull"] and structure1 == "HH/HL" and e21_1 is not None and e50_1 is not None and price > 0 and close1[-1] > e50_1 and e21_1 >= e50_1 and protected1["state"] == "BULLISH"
    short1 = regime4["bear"] and structure1 == "LH/LL" and e21_1 is not None and e50_1 is not None and close1[-1] < e50_1 and e21_1 <= e50_1 and protected1["state"] == "BEARISH"

    atr15 = _safe_atr(c15); r15 = _safe_rsi(close15); rv15 = _relative_volume(c15); vol15 = volume_status(c15)
    bos_long, ret_long = _select_latest_bos_with_retest(c15, "LONG")
    bos_short, ret_short = _select_latest_bos_with_retest(c15, "SHORT")
    long_candidate = bool(long1 and bos_long and ret_long["valid"])
    short_candidate = bool(short1 and bos_short and ret_short["valid"])

    trigger_side = "LONG" if long_candidate and not short_candidate else "SHORT" if short_candidate and not long_candidate else "NONE"
    active_bos = bos_long if trigger_side == "LONG" else bos_short if trigger_side == "SHORT" else None
    active_retest = ret_long if trigger_side == "LONG" else ret_short if trigger_side == "SHORT" else None
    trigger_level = float(active_bos["level"]) if active_bos else None
    trigger = _five_minute_trigger(c5, trigger_side, trigger_level)
    if active_retest and trigger["ready"] and trigger["candle_time"] < int(active_retest["time"]):
        trigger = dict(trigger); trigger["ready"] = False; trigger["long"] = False; trigger["short"] = False
    setup = "LONG" if trigger_side == "LONG" and trigger["long"] else "SHORT" if trigger_side == "SHORT" and trigger["short"] else "NO TRADE"

    if setup in {"LONG", "SHORT"} and active_retest and trigger["candle_time"] - int(active_retest["time"]) > 20 * 60 * 1000:
        setup = "NO TRADE"

    support, resistance = _level_clusters(c15, atr15)
    try:
        sr_support, sr_resistance = get_support_resistance(c15)
        support = sr_support if sr_support is not None else support
        resistance = sr_resistance if sr_resistance is not None else resistance
    except Exception:
        pass

    atr_pct = _atr_percent(price, atr15)
    atr_rank = _atr_percentile(c15)
    volatility_ok = bool(atr15 > 0 and MIN_ATR_PERCENTILE <= atr_rank <= MAX_ATR_PERCENTILE and 0.001 <= atr_pct <= 0.05)
    macd_line, macd_signal, macd_hist = _macd(close15)
    momentum_ok = bool((setup == "LONG" and 50 < r15 < 75 and macd_hist >= 0) or (setup == "SHORT" and 25 < r15 < 50 and macd_hist <= 0))
    volume_ok = bool(rv15 >= 1.0 and trigger["rvol"] >= 1.0)

    data_for_levels = {
        "setup": setup,
        "price": price,
        "atr": atr15,
        "protected_low": protected1.get("protected_low"),
        "protected_high": protected1.get("protected_high"),
        "support": support,
        "resistance": resistance,
        "retest": active_retest or {},
        "target_frames": [("1D", c1d), ("4H", c4), ("1H", c1), ("15M", c15)],
        "_candles_15m": c15,
    }
    levels = calculate_trade_levels(data_for_levels)
    sl = levels.get("stop_loss")
    risk = abs(price - sl) if sl is not None else 0.0
    sl_atr = risk / atr15 if atr15 > 0 else 999.0
    entry_distance = abs(price - float(active_retest.get("level"))) / atr15 if active_retest and atr15 > 0 else 0.0
    location_ok = bool(levels.get("target_path_ok") and levels.get("target_path_structural") and entry_distance <= MAX_ENTRY_DISTANCE_ATR)
    if setup in {"LONG", "SHORT"} and not (MIN_SL_ATR <= sl_atr <= MAX_SL_ATR):
        location_ok = False
    rr = levels.get("rr")
    risk_ok = bool(setup in {"LONG", "SHORT"} and sl is not None and levels.get("tp2") is not None and rr is not None and rr >= MIN_RR)

    direction_ok = bool((setup == "LONG" and long1 and regime4["bull"]) or (setup == "SHORT" and short1 and regime4["bear"]))
    structure_ok = bool((setup == "LONG" and structure1 == "HH/HL" and protected1["state"] == "BULLISH" and bos_long and ret_long["valid"]) or (setup == "SHORT" and structure1 == "LH/LL" and protected1["state"] == "BEARISH" and bos_short and ret_short["valid"]))
    setup_ok = bool((setup == "LONG" and trigger["long"] and long_candidate) or (setup == "SHORT" and trigger["short"] and short_candidate)) and risk_ok

    score, groups, families = _build_score(
        direction_ok=direction_ok,
        structure_ok=structure_ok,
        setup_ok=setup_ok,
        momentum_ok=momentum_ok,
        volume_ok=volume_ok,
        location_ok=location_ok,
        futures_ok=False,
        volatility_ok=volatility_ok,
        trigger_quality=_num(trigger.get("quality")),
        rvol=rv15,
        bos_quality=_num((active_bos or {}).get("strength")),
        retest_quality=_num((active_retest or {}).get("quality")),
    )

    ema_direction = "BULLISH" if (_safe_ema(close15, 21) or 0) > (_safe_ema(close15, 50) or 0) else "BEARISH" if (_safe_ema(close15, 21) or 0) < (_safe_ema(close15, 50) or 0) else "NEUTRAL"
    # Direction + structure + valid setup/risk are mandatory. Structural
    # target path and volatility remain hard safety gates. Momentum/volume
    # stay as confirmation families and are enforced by the final validator
    # through the 5/6 family rule rather than being all-or-nothing here.
    technical_candidate = bool(
        setup in {"LONG", "SHORT"}
        and direction_ok
        and structure_ok
        and setup_ok
        and location_ok
        and volatility_ok
    )
    signal_blocked = not technical_candidate

    technical_failures: list[str] = []
    if not regime4["bull"] and not regime4["bear"]:
        technical_failures.append("4H regime")
    if not long1 and not short1:
        technical_failures.append("1H alignment")
    long_events = _bos_events(c15, "LONG")
    short_events = _bos_events(c15, "SHORT")
    if not bos_long and not bos_short:
        if not long_events and not short_events:
            technical_failures.append("15M BOS")
        else:
            technical_failures.append("15M post-BOS retest")
    if trigger_side != "NONE" and not trigger.get("ready"):
        technical_failures.append("5M trigger")
    if setup in {"LONG", "SHORT"} and not momentum_ok:
        technical_failures.append("momentum")
    if setup in {"LONG", "SHORT"} and not volume_ok:
        technical_failures.append("volume/RVOL")
    if setup in {"LONG", "SHORT"} and not location_ok:
        technical_failures.append("target path/location")
    if setup in {"LONG", "SHORT"} and not volatility_ok:
        technical_failures.append("volatility")

    reasons: list[str] = []
    if regime4["bull"]: reasons.append("4H bullish regime")
    if regime4["bear"]: reasons.append("4H bearish regime")
    if get_structure(c1d) != "UNKNOWN": reasons.append(f"1D structure {get_structure(c1d)}") if len(c1d) >= 20 else None
    if long1: reasons.append("1H bullish alignment")
    if short1: reasons.append("1H bearish alignment")
    if active_bos: reasons.append(f"15M {trigger_side} BOS confirmed")
    if active_retest and active_retest.get("valid"): reasons.append(f"15M {trigger_side} post-BOS retest")
    if trigger.get("ready"): reasons.append("5M trigger confirmed")
    if momentum_ok: reasons.append("Momentum aligned")
    if volume_ok: reasons.append("Volume/RVOL aligned")
    if location_ok: reasons.append("Structural target path acceptable")
    if volatility_ok: reasons.append("Volatility gate acceptable")
    if not technical_candidate and setup != "NO TRADE": reasons.append("Technical hard gate failed")

    return {
        "symbol": symbol,
        "price": price,
        "setup": setup,
        "setup_candidate": setup if setup in {"LONG", "SHORT"} else "NO TRADE",
        "trend_4h": "BULLISH" if regime4["bull"] else "BEARISH" if regime4["bear"] else "NO_TRADE",
        "regime": regime4["regime"],
        "daily_structure_1d": get_structure(c1d) if len(c1d) >= 20 else "UNAVAILABLE",
        "structure_1h": structure1,
        "protected_structure_1h": protected1["state"],
        "protected_high": protected1.get("protected_high"),
        "protected_low": protected1.get("protected_low"),
        "bos_15m": bool(active_bos),
        "bos_15m_time": (active_bos or {}).get("time"),
        "bos_15m_index": (active_bos or {}).get("index"),
        "bos_15m_strength": _num((active_bos or {}).get("strength")),
        "long_bos_level": bos_long.get("level") if bos_long else None,
        "short_bos_level": bos_short.get("level") if bos_short else None,
        "long_bos_event_count": len(long_events),
        "short_bos_event_count": len(short_events),
        "long_retest": bool(ret_long.get("valid")),
        "short_retest": bool(ret_short.get("valid")),
        "long_retest_time": ret_long.get("time"),
        "short_retest_time": ret_short.get("time"),
        "retest": active_retest or {},
        "ema21": _safe_ema(close15, 21),
        "ema50": _safe_ema(close15, 50),
        "ema21_4h": regime4["e21"], "ema50_4h": regime4["e50"], "ema100_4h": regime4["e100"], "ema200_4h": regime4["e200"],
        "ema21_1h": e21_1, "ema50_1h": e50_1, "ema_direction": ema_direction,
        "rsi": r15, "rsi_5m": trigger["rsi"],
        "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
        "atr": atr15, "atr_4h": regime4["atr"], "atr_5m": trigger["atr"], "atr_pct": atr_pct,
        "atr_percentile": atr_rank, "adx_4h": regime4["adx"], "ema50_slope_4h": regime4["slope"],
        "volume": vol15, "rvol": rv15, "rvol_15m": rv15, "rvol_5m": trigger["rvol"],
        "support": support, "resistance": resistance,
        "futures_context": "PENDING", "futures_ok": False,
        "btc_filter_ok": False, "btc_filter_reason": "PENDING",
        "data_fresh": True,
        "signal_engine_version": ENGINE_VERSION,
        "trigger_5m": "CONFIRMED" if trigger.get("ready") else "NONE",
        "trigger_quality_5m": trigger["quality"], "trigger_quality": trigger["quality"],
        "five_minute_ready": trigger["ready"], "five_minute_long": trigger["long"], "five_minute_short": trigger["short"],
        "closed_5m_candle_time": trigger["candle_time"],
        "score": score, "score_groups": groups, "confirmation_family_count": families,
        "bullish_points": int(regime4["bull"]) + int(structure1 == "HH/HL") + int(e21_1 is not None and e50_1 is not None and e21_1 >= e50_1),
        "bearish_points": int(regime4["bear"]) + int(structure1 == "LH/LL") + int(e21_1 is not None and e50_1 is not None and e21_1 <= e50_1),
        "direction_ok": direction_ok, "structure_ok": structure_ok, "setup_ok": setup_ok,
        "momentum_ok": momentum_ok, "volume_ok": volume_ok, "location_ok": location_ok,
        "volatility_ok": volatility_ok, "risk_ok": risk_ok,
        "sl_atr": sl_atr, "entry_distance_atr": entry_distance,
        "target_path_ok": bool(levels.get("target_path_ok")), "target_path_structural": bool(levels.get("target_path_structural")),
        "target_path_reason": levels.get("target_path_reason"), "target_obstacle": levels.get("target_obstacle"),
        "technical_candidate": technical_candidate, "signal_blocked": signal_blocked,
        "rejection_stage": None if technical_candidate else "TECHNICAL",
        "technical_gate_failures": technical_failures,
        "reasons": reasons,
        "candle_time": int(c15[-1]["time"]),
        "setup_bos_time": (active_bos or {}).get("time"),
        "setup_retest_time": (active_retest or {}).get("time"),
        **levels,
    }


async def analyze_symbol(market, symbol: str) -> Dict[str, Any]:
    ref = await market.resolve(symbol)
    c1d = await market.ohlcv(ref, "1D", 60)
    c4h = await market.ohlcv(ref, "4H", 250)
    c1h = await market.ohlcv(ref, "1H", 250)
    c15 = await market.ohlcv(ref, "15M", 250)
    c5 = await market.ohlcv(ref, "5M", 250)
    return analyze_candles(ref.symbol, c4h, c1h, c15, c5, c1d)
