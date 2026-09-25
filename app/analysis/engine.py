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
ENGINE_VERSION = "gold-v1.4-deterministic-diagnostics"


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
    """Select the newest fresh BOS that has a strictly post-BOS retest.

    The returned retest object always contains a diagnostic failure when no
    valid retest is found. This keeps the engine explainable without changing
    the actual entry gates.
    """
    events = _bos_events(candles, side)
    latest_index = len(candles) - 1
    if not events:
        return None, {
            "valid": False,
            "index": None,
            "time": None,
            "level": None,
            "quality": 0.0,
            "rejection": False,
            "low": None,
            "high": None,
            "failure": "no confirmed 15M BOS",
        }

    last_failure = "no fresh post-BOS retest"
    for bos in reversed(events):
        bos_age = latest_index - int(bos["index"])
        if bos_age > MAX_SETUP_AGE_15M + 2:
            last_failure = "15M BOS is stale"
            continue

        retest = _pullback_retest(candles, side, bos, max_bars=MAX_SETUP_AGE_15M)
        if not retest["valid"]:
            last_failure = "no valid post-BOS retest"
            continue

        retest_age = latest_index - int(retest["index"])
        if retest_age > MAX_SETUP_AGE_15M:
            last_failure = "post-BOS retest is stale"
            continue

        # Explicit chronology check. The retest can never be the BOS candle.
        if int(retest["index"]) <= int(bos["index"]):
            last_failure = "retest is not after BOS"
            continue

        retest["bos_index"] = int(bos["index"])
        retest["bos_time"] = int(bos["time"])
        retest["post_bos_bars"] = int(retest["index"]) - int(bos["index"])
        return bos, retest

    return None, {
        "valid": False,
        "index": None,
        "time": None,
        "level": None,
        "quality": 0.0,
        "rejection": False,
        "low": None,
        "high": None,
        "failure": last_failure,
    }



def _five_minute_trigger(
    candles: List[Candle],
    side: str,
    setup_level: Optional[float],
    *,
    required_after_time: Optional[int] = None,
    max_age_ms: int = 20 * 60 * 1000,
) -> Dict[str, Any]:
    """Validate the final 5M trigger after a confirmed 15M retest.

    All checks use closed 5M candles. A trigger is invalid when it precedes
    the retest or arrives too late, which makes the full sequence explicit:
    BOS -> retest -> 5M trigger.
    """
    empty = {
        "ready": False,
        "long": False,
        "short": False,
        "quality": 0.0,
        "rsi": 50.0,
        "rvol": 0.0,
        "atr": 0.0,
        "candle_time": 0,
        "body_ratio": 0.0,
        "failure": "insufficient 5M candles",
        "conditions": {},
    }
    if len(candles) < 30:
        return empty

    c = candles[-1]
    prev = candles[-2]
    candle_time = int(c["time"])
    close = float(c["close"])
    open_price = float(c["open"])
    high = float(c["high"])
    low = float(c["low"])
    prev_high = float(prev["high"])
    prev_low = float(prev["low"])
    rng = max(high - low, 1e-12)
    body_ratio = abs(close - open_price) / rng
    r = _safe_rsi([float(x["close"]) for x in candles])
    rv = _relative_volume(candles)
    a = _safe_atr(candles)
    bullish_body = close > open_price
    bearish_body = close < open_price

    sequence_after_retest = True if required_after_time is None else candle_time > int(required_after_time)
    sequence_fresh = True if required_after_time is None else candle_time - int(required_after_time) <= max_age_ms
    level_long_ok = setup_level is None or close > float(setup_level)
    level_short_ok = setup_level is None or close < float(setup_level)

    long_conditions = {
        "bullish_body": bullish_body,
        "close_above_previous_high": close > prev_high,
        "close_above_setup_level": level_long_ok,
        "rsi_above_50": r > 50.0,
        "rvol_at_least_1": rv >= 1.0,
        "body_ratio_at_least_0_55": body_ratio >= 0.55,
        "after_15m_retest": sequence_after_retest,
        "within_retest_trigger_window": sequence_fresh,
    }
    short_conditions = {
        "bearish_body": bearish_body,
        "close_below_previous_low": close < prev_low,
        "close_below_setup_level": level_short_ok,
        "rsi_below_50": r < 50.0,
        "rvol_at_least_1": rv >= 1.0,
        "body_ratio_at_least_0_55": body_ratio >= 0.55,
        "after_15m_retest": sequence_after_retest,
        "within_retest_trigger_window": sequence_fresh,
    }

    long_ok = all(long_conditions.values())
    short_ok = all(short_conditions.values())

    if side == "LONG":
        strength_rsi = max(0.0, min((r - 50.0) / 15.0, 1.0))
    elif side == "SHORT":
        strength_rsi = max(0.0, min((50.0 - r) / 15.0, 1.0))
    else:
        strength_rsi = 0.0

    quality = min(
        1.0,
        0.35 * min(body_ratio / 0.70, 1.0)
        + 0.35 * min(rv / 1.50, 1.0)
        + 0.30 * strength_rsi,
    )

    if side == "LONG":
        relevant = long_conditions
    elif side == "SHORT":
        relevant = short_conditions
    else:
        relevant = {"valid_side": False}

    failure = None
    if side in {"LONG", "SHORT"} and not all(relevant.values()):
        first_failed = next((name for name, ok in relevant.items() if not ok), "unknown")
        failure = f"5M trigger failed: {first_failed}"
    elif side not in {"LONG", "SHORT"}:
        failure = "5M trigger side unavailable"

    return {
        "ready": bool(long_ok if side == "LONG" else short_ok if side == "SHORT" else False),
        "long": bool(long_ok),
        "short": bool(short_ok),
        "quality": quality,
        "rsi": r,
        "rvol": rv,
        "atr": a,
        "candle_time": candle_time,
        "body_ratio": body_ratio,
        "failure": failure,
        "conditions": {"LONG": long_conditions, "SHORT": short_conditions},
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
    """Evaluate the strict V1 4H regime with auditable sub-gates.

    Gold Standard V1 bullish regime:
    close > EMA200, EMA21 > EMA50 > EMA100 > EMA200, EMA50 slope > 0,
    ADX >= 25, and the protected swing low remains intact.

    The bearish side is symmetric. No weak/neutral regime is promoted into a
    signal; the detailed component checks are returned for diagnostics.
    """
    close = [float(c["close"]) for c in candles]
    e21 = _safe_ema(close, 21)
    e50 = _safe_ema(close, 50)
    e100 = _safe_ema(close, 100)
    e200 = _safe_ema(close, 200)
    atr4 = _safe_atr(candles)
    adx4 = _adx(candles)
    slope4 = _ema_slope(close, 50)
    protected = _protected_structure(candles)
    last_close = close[-1] if close else 0.0

    values_ready = all(x is not None for x in (e21, e50, e100, e200)) and bool(candles)
    bullish_components = {
        "close_above_ema200": bool(values_ready and last_close > float(e200)),
        "ema_stack_21_50_100_200": bool(values_ready and e21 > e50 > e100 > e200),
        "ema50_slope_positive": bool(values_ready and slope4 > 0),
        "adx_at_least_25": bool(adx4 >= 25),
        "protected_low_exists": protected.get("protected_low") is not None,
        "protected_low_intact": bool(
            protected.get("protected_low") is not None
            and last_close > float(protected["protected_low"])
        ),
    }
    bearish_components = {
        "close_below_ema200": bool(values_ready and last_close < float(e200)),
        "ema_stack_21_50_100_200": bool(values_ready and e21 < e50 < e100 < e200),
        "ema50_slope_negative": bool(values_ready and slope4 < 0),
        "adx_at_least_25": bool(adx4 >= 25),
        "protected_high_exists": protected.get("protected_high") is not None,
        "protected_high_intact": bool(
            protected.get("protected_high") is not None
            and last_close < float(protected["protected_high"])
        ),
    }

    bull = bool(values_ready and all(bullish_components.values()))
    bear = bool(values_ready and all(bearish_components.values()))
    if bull and not bear:
        regime = "BULLISH"
    elif bear and not bull:
        regime = "BEARISH"
    else:
        regime = "NO_TRADE"

    failed_bull = [k for k, ok in bullish_components.items() if not ok]
    failed_bear = [k for k, ok in bearish_components.items() if not ok]
    return {
        "bull": bull,
        "bear": bear,
        "regime": regime,
        "e21": e21,
        "e50": e50,
        "e100": e100,
        "e200": e200,
        "atr": atr4,
        "adx": adx4,
        "slope": slope4,
        "protected": protected,
        "components": {"BULLISH": bullish_components, "BEARISH": bearish_components},
        "failed_components": {"BULLISH": failed_bull, "BEARISH": failed_bear},
        "protected_low_intact": bool(bullish_components.get("protected_low_intact")),
        "protected_high_intact": bool(bearish_components.get("protected_high_intact")),
    }



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
    """Run the complete deterministic Gold Standard technical hierarchy.

    This function does not fabricate futures context. It deliberately stops at
    a *technical candidate*; the scanner later adds live MEXC quote, BTC,
    order-book, trade-flow and futures-context evidence before final validation.
    """
    now_ms = int(time.time() * 1000)
    c4 = closed_candle_rows(candles_4h, "4h", now_ms)
    c1 = closed_candle_rows(candles_1h, "1h", now_ms)
    c15 = closed_candle_rows(candles_15m, "15m", now_ms)
    c5 = closed_candle_rows(candles_5m or [], "5m", now_ms)
    c1d = closed_candle_rows(candles_1d or [], "1d", now_ms)

    quality_results: Dict[str, Dict[str, Any]] = {}
    for candles, tf, minimum in (
        (c4, "4h", 205),
        (c1, "1h", 205),
        (c15, "15m", 80),
        (c5, "5m", 30),
    ):
        ok, reason = _data_quality(candles, TIMEFRAME_MS[tf], minimum)
        quality_results[tf.upper()] = {"ok": ok, "reason": reason, "count": len(candles)}
        if not ok:
            raise ValueError(f"{symbol}: {reason}")

    close4 = [float(c["close"]) for c in c4]
    close1 = [float(c["close"]) for c in c1]
    close15 = [float(c["close"]) for c in c15]
    price = close15[-1]

    # ================================================================
    # 4H REGIME — strict Gold Standard, fully diagnosed
    # ================================================================
    regime4 = _four_hour_regime(c4)

    # ================================================================
    # 1H DIRECTION + STRUCTURE — no extra hidden protected-state gate
    # beyond the documented HH/HL or LH/LL structure requirement.
    # ================================================================
    structure1 = get_structure(c1)
    protected1 = _protected_structure(c1)
    e21_1 = _safe_ema(close1, 21)
    e50_1 = _safe_ema(close1, 50)
    last_close1 = close1[-1]

    long_1h_components = {
        "4h_bullish_regime": bool(regime4["bull"]),
        "1h_hh_hl": structure1 == "HH/HL",
        "1h_close_above_ema50": bool(e50_1 is not None and last_close1 > e50_1),
        "1h_ema21_at_or_above_ema50": bool(e21_1 is not None and e50_1 is not None and e21_1 >= e50_1),
    }
    short_1h_components = {
        "4h_bearish_regime": bool(regime4["bear"]),
        "1h_lh_ll": structure1 == "LH/LL",
        "1h_close_below_ema50": bool(e50_1 is not None and last_close1 < e50_1),
        "1h_ema21_at_or_below_ema50": bool(e21_1 is not None and e50_1 is not None and e21_1 <= e50_1),
    }
    long1 = all(long_1h_components.values())
    short1 = all(short_1h_components.values())

    # Informational only: current protected 1H state is recorded, but it is
    # not an extra hidden veto on top of the documented HH/HL / LH/LL gate.
    protected_1h_direction = protected1["state"]

    # ================================================================
    # 15M SETUP — BOS then strictly post-BOS retest
    # ================================================================
    atr15 = _safe_atr(c15)
    r15 = _safe_rsi(close15)
    rv15 = _relative_volume(c15)
    vol15 = volume_status(c15)
    bos_long, ret_long = _select_latest_bos_with_retest(c15, "LONG")
    bos_short, ret_short = _select_latest_bos_with_retest(c15, "SHORT")

    long_structure_candidate = bool(long1 and bos_long and ret_long.get("valid"))
    short_structure_candidate = bool(short1 and bos_short and ret_short.get("valid"))

    # ================================================================
    # 5M ENTRY — calculated independently per side for diagnostics, but
    # only a side with valid 4H + 1H + BOS + retest can become a setup.
    # ================================================================
    trigger_long_level = float(bos_long["level"]) if bos_long else None
    trigger_short_level = float(bos_short["level"]) if bos_short else None
    trigger_long = _five_minute_trigger(
        c5,
        "LONG",
        trigger_long_level,
        required_after_time=ret_long.get("time") if ret_long.get("valid") else None,
        max_age_ms=20 * 60 * 1000,
    )
    trigger_short = _five_minute_trigger(
        c5,
        "SHORT",
        trigger_short_level,
        required_after_time=ret_short.get("time") if ret_short.get("valid") else None,
        max_age_ms=20 * 60 * 1000,
    )

    long_sequence_ok = bool(
        long_structure_candidate
        and trigger_long.get("ready")
        and int(trigger_long.get("candle_time", 0)) > int(ret_long.get("time") or 0)
    )
    short_sequence_ok = bool(
        short_structure_candidate
        and trigger_short.get("ready")
        and int(trigger_short.get("candle_time", 0)) > int(ret_short.get("time") or 0)
    )

    trigger_side = "LONG" if long_sequence_ok and not short_sequence_ok else "SHORT" if short_sequence_ok and not long_sequence_ok else "NONE"
    active_bos = bos_long if trigger_side == "LONG" else bos_short if trigger_side == "SHORT" else None
    active_retest = ret_long if trigger_side == "LONG" else ret_short if trigger_side == "SHORT" else None
    trigger = trigger_long if trigger_side == "LONG" else trigger_short if trigger_side == "SHORT" else {
        "ready": False,
        "long": bool(trigger_long.get("ready")),
        "short": bool(trigger_short.get("ready")),
        "quality": max(_num(trigger_long.get("quality")), _num(trigger_short.get("quality"))),
        "rsi": _safe_rsi([float(x["close"]) for x in c5]),
        "rvol": _relative_volume(c5),
        "atr": _safe_atr(c5),
        "candle_time": int(c5[-1]["time"]),
        "body_ratio": max(_num(trigger_long.get("body_ratio")), _num(trigger_short.get("body_ratio"))),
        "failure": "no side passed the complete BOS -> retest -> 5M sequence",
        "conditions": {"LONG": trigger_long.get("conditions", {}), "SHORT": trigger_short.get("conditions", {})},
    }
    setup = "LONG" if trigger_side == "LONG" and trigger_long["ready"] else "SHORT" if trigger_side == "SHORT" and trigger_short["ready"] else "NO TRADE"

    # ================================================================
    # 15M/5M freshness: the trigger must be close to the retest.
    # ================================================================
    setup_fresh = True
    setup_fresh_reason = "not applicable"
    if setup in {"LONG", "SHORT"} and active_retest:
        lag = int(trigger["candle_time"]) - int(active_retest["time"])
        setup_fresh = bool(0 < lag <= 20 * 60 * 1000)
        setup_fresh_reason = "OK" if setup_fresh else "5M trigger is stale or precedes retest"
        if not setup_fresh:
            setup = "NO TRADE"

    # ================================================================
    # Location / momentum / volume / volatility
    # ================================================================
    support, resistance = _level_clusters(c15, atr15)
    try:
        sr_support, sr_resistance = get_support_resistance(c15)
        support = sr_support if sr_support is not None else support
        resistance = sr_resistance if sr_resistance is not None else resistance
    except Exception:
        pass

    atr_pct = _atr_percent(price, atr15)
    atr_rank = _atr_percentile(c15)
    volatility_components = {
        "atr_available": atr15 > 0,
        "atr_percentile_in_range": bool(MIN_ATR_PERCENTILE <= atr_rank <= MAX_ATR_PERCENTILE),
        "atr_percent_reasonable": bool(0.001 <= atr_pct <= 0.05),
    }
    volatility_ok = bool(all(volatility_components.values()))

    macd_line, macd_signal, macd_hist = _macd(close15)
    momentum_long_components = {
        "rsi_50_to_75": 50 < r15 < 75,
        "macd_non_negative": macd_hist >= 0,
    }
    momentum_short_components = {
        "rsi_25_to_50": 25 < r15 < 50,
        "macd_non_positive": macd_hist <= 0,
    }
    momentum_long_ok = bool(all(momentum_long_components.values()))
    momentum_short_ok = bool(all(momentum_short_components.values()))
    momentum_ok = momentum_long_ok if setup == "LONG" else momentum_short_ok if setup == "SHORT" else False

    volume_long_components = {
        "rvol_15m_at_least_1": rv15 >= 1.0,
        "rvol_5m_at_least_1": _num(trigger_long.get("rvol")) >= 1.0,
    }
    volume_short_components = {
        "rvol_15m_at_least_1": rv15 >= 1.0,
        "rvol_5m_at_least_1": _num(trigger_short.get("rvol")) >= 1.0,
    }
    volume_long_ok = bool(all(volume_long_components.values()))
    volume_short_ok = bool(all(volume_short_components.values()))
    volume_ok = volume_long_ok if setup == "LONG" else volume_short_ok if setup == "SHORT" else False

    momentum_components = {
        "LONG": momentum_long_components,
        "SHORT": momentum_short_components,
    }
    volume_components = {
        "LONG": volume_long_components,
        "SHORT": volume_short_components,
    }

    # ================================================================
    # Stop + target path. Compute both sides for diagnostics, then select
    # the active side without fabricating TP1/TP2.
    # ================================================================
    target_frames = [("1D", c1d), ("4H", c4), ("1H", c1), ("15M", c15)]
    common_level_data = {
        "price": price,
        "atr": atr15,
        "protected_low": protected1.get("protected_low"),
        "protected_high": protected1.get("protected_high"),
        "support": support,
        "resistance": resistance,
        "target_frames": target_frames,
        "_candles_15m": c15,
    }
    long_levels = calculate_trade_levels({**common_level_data, "setup": "LONG", "retest": ret_long if ret_long.get("valid") else {}})
    short_levels = calculate_trade_levels({**common_level_data, "setup": "SHORT", "retest": ret_short if ret_short.get("valid") else {}})
    levels = long_levels if setup == "LONG" else short_levels if setup == "SHORT" else {
        "entry": price, "stop_loss": None, "tp1": None, "tp2": None, "rr": None,
        "target_path_ok": False, "target_path_structural": False,
        "target_path_reason": "No active LONG/SHORT setup", "target_obstacle": None,
        "target_levels": [],
    }

    def side_level_flags(side_levels: Dict[str, Any], retest: Dict[str, Any], side_name: str) -> Dict[str, Any]:
        stop_value = side_levels.get("stop_loss")
        side_risk = abs(price - float(stop_value)) if stop_value is not None else 0.0
        side_sl_atr = side_risk / atr15 if atr15 > 0 else 999.0
        side_entry_distance = (
            abs(price - float(retest.get("level"))) / atr15
            if retest and retest.get("level") is not None and atr15 > 0
            else 999.0
        )
        side_target_ok = bool(side_levels.get("target_path_ok") and side_levels.get("target_path_structural"))
        side_sl_ok = bool(MIN_SL_ATR <= side_sl_atr <= MAX_SL_ATR) if stop_value is not None else False
        side_location_ok = bool(side_target_ok and side_entry_distance <= MAX_ENTRY_DISTANCE_ATR and side_sl_ok)
        side_rr = side_levels.get("rr")
        side_risk_ok = bool(
            stop_value is not None
            and side_levels.get("tp2") is not None
            and side_rr is not None
            and float(side_rr) >= MIN_RR
        )
        return {
            "side": side_name,
            "sl_atr": side_sl_atr,
            "sl_atr_ok": side_sl_ok,
            "entry_distance_atr": side_entry_distance,
            "target_path_ok": side_target_ok,
            "location_ok": side_location_ok,
            "rr": side_rr,
            "risk_ok": side_risk_ok,
        }

    long_level_flags = side_level_flags(long_levels, ret_long if ret_long.get("valid") else {}, "LONG")
    short_level_flags = side_level_flags(short_levels, ret_short if ret_short.get("valid") else {}, "SHORT")

    sl = levels.get("stop_loss")
    risk = abs(price - sl) if sl is not None else 0.0
    sl_atr = risk / atr15 if atr15 > 0 else 999.0
    entry_distance = (
        abs(price - float(active_retest.get("level"))) / atr15
        if active_retest and active_retest.get("level") is not None and atr15 > 0
        else 999.0
    )
    sl_atr_ok = bool(setup in {"LONG", "SHORT"} and MIN_SL_ATR <= sl_atr <= MAX_SL_ATR)
    target_path_ok = bool(levels.get("target_path_ok") and levels.get("target_path_structural"))
    location_ok = bool(target_path_ok and entry_distance <= MAX_ENTRY_DISTANCE_ATR and sl_atr_ok)
    rr = levels.get("rr")
    risk_ok = bool(
        setup in {"LONG", "SHORT"}
        and sl is not None
        and levels.get("tp2") is not None
        and rr is not None
        and rr >= MIN_RR
    )

    # ================================================================
    # Direction / structure / setup hard gates
    # ================================================================
    direction_ok = bool(
        (setup == "LONG" and long1 and regime4["bull"])
        or (setup == "SHORT" and short1 and regime4["bear"])
    )
    structure_ok = bool(
        (setup == "LONG" and bos_long and ret_long.get("valid"))
        or (setup == "SHORT" and bos_short and ret_short.get("valid"))
    )
    setup_ok = bool(
        (setup == "LONG" and trigger_long.get("ready") and long_sequence_ok)
        or (setup == "SHORT" and trigger_short.get("ready") and short_sequence_ok)
    ) and risk_ok

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

    ema21_15 = _safe_ema(close15, 21)
    ema50_15 = _safe_ema(close15, 50)
    ema_direction = (
        "BULLISH" if (ema21_15 or 0) > (ema50_15 or 0)
        else "BEARISH" if (ema21_15 or 0) < (ema50_15 or 0)
        else "NEUTRAL"
    )

    # Technical candidate = hard technical hierarchy only. Final score and
    # 5/6 family rule are applied again by the scanner/validator after live
    # MEXC context is attached.
    technical_candidate = bool(
        setup in {"LONG", "SHORT"}
        and direction_ok
        and structure_ok
        and setup_ok
        and location_ok
        and volatility_ok
    )
    signal_blocked = not technical_candidate

    # ================================================================
    # Detailed diagnostics — do not change gates, only expose them.
    # ================================================================
    long_bos_events = _bos_events(c15, "LONG")
    short_bos_events = _bos_events(c15, "SHORT")

    long_gates = {
        "4H regime": regime4["bull"],
        "1H alignment": long1,
        "15M BOS": bool(bos_long),
        "15M post-BOS retest": bool(ret_long.get("valid")),
        "5M trigger": bool(trigger_long.get("ready")),
        "BOS->retest->5M sequence": long_sequence_ok,
        "setup freshness": bool(
            trigger_long.get("candle_time", 0) > int(ret_long.get("time") or 0)
            and int(trigger_long.get("candle_time", 0)) - int(ret_long.get("time") or 0) <= 20 * 60 * 1000
        ) if ret_long.get("valid") else False,
        "momentum": momentum_long_ok,
        "volume/RVOL": volume_long_ok,
        "target path": bool(long_level_flags["target_path_ok"]),
        "entry distance": bool(long_level_flags["entry_distance_atr"] <= MAX_ENTRY_DISTANCE_ATR),
        "SL ATR": bool(long_level_flags["sl_atr_ok"]),
        "RR >= 2.0": bool(long_level_flags["risk_ok"]),
        "volatility": volatility_ok,
        "technical hard gate": technical_candidate if setup == "LONG" else False,
    }
    short_gates = {
        "4H regime": regime4["bear"],
        "1H alignment": short1,
        "15M BOS": bool(bos_short),
        "15M post-BOS retest": bool(ret_short.get("valid")),
        "5M trigger": bool(trigger_short.get("ready")),
        "BOS->retest->5M sequence": short_sequence_ok,
        "setup freshness": bool(
            trigger_short.get("candle_time", 0) > int(ret_short.get("time") or 0)
            and int(trigger_short.get("candle_time", 0)) - int(ret_short.get("time") or 0) <= 20 * 60 * 1000
        ) if ret_short.get("valid") else False,
        "momentum": momentum_short_ok,
        "volume/RVOL": volume_short_ok,
        "target path": bool(short_level_flags["target_path_ok"]),
        "entry distance": bool(short_level_flags["entry_distance_atr"] <= MAX_ENTRY_DISTANCE_ATR),
        "SL ATR": bool(short_level_flags["sl_atr_ok"]),
        "RR >= 2.0": bool(short_level_flags["risk_ok"]),
        "volatility": volatility_ok,
        "technical hard gate": technical_candidate if setup == "SHORT" else False,
    }

    # Side-neutral gate diagnostics are the primary output when there is not
    # yet a side-specific setup. This is what scanner aggregation can use to
    # see exactly what is killing the universe without weakening the engine.
    hierarchy = [
        "4H regime",
        "1H alignment",
        "15M BOS",
        "15M post-BOS retest",
        "5M trigger",
        "BOS->retest->5M sequence",
        "setup freshness",
        "target path",
        "entry distance",
        "SL ATR",
        "RR >= 2.0",
        "volatility",
        "momentum",
        "volume/RVOL",
    ]

    if setup == "LONG":
        technical_failures = [name for name in hierarchy if not long_gates[name]]
        primary_failure = technical_failures[0] if technical_failures else None
    elif setup == "SHORT":
        technical_failures = [name for name in hierarchy if not short_gates[name]]
        primary_failure = technical_failures[0] if technical_failures else None
    else:
        # For NO TRADE, report the complete side-specific blockers. The first
        # failing gate on each side gives the exact earliest bottleneck.
        long_failed = [name for name in hierarchy if not long_gates[name]]
        short_failed = [name for name in hierarchy if not short_gates[name]]
        technical_failures = []
        if long_failed:
            technical_failures.append(f"LONG: {long_failed[0]}")
        if short_failed:
            technical_failures.append(f"SHORT: {short_failed[0]}")
        if not technical_failures:
            technical_failures.append("No side completed the full sequence")
        primary_failure = technical_failures[0]

    four_h_failures = {
        "LONG": list(regime4["failed_components"]["BULLISH"]),
        "SHORT": list(regime4["failed_components"]["BEARISH"]),
    }
    one_h_failures = {
        "LONG": [k for k, ok in long_1h_components.items() if not ok],
        "SHORT": [k for k, ok in short_1h_components.items() if not ok],
    }

    sequence = {
        "LONG": {
            "bos": bool(bos_long),
            "bos_time": (bos_long or {}).get("time"),
            "retest": bool(ret_long.get("valid")),
            "retest_time": ret_long.get("time"),
            "trigger": bool(trigger_long.get("ready")),
            "trigger_time": trigger_long.get("candle_time"),
            "chronology_valid": bool(
                bos_long
                and ret_long.get("valid")
                and int(ret_long.get("index") or -1) > int(bos_long.get("index") or -1)
                and int(trigger_long.get("candle_time") or 0) > int(ret_long.get("time") or 0)
            ),
        },
        "SHORT": {
            "bos": bool(bos_short),
            "bos_time": (bos_short or {}).get("time"),
            "retest": bool(ret_short.get("valid")),
            "retest_time": ret_short.get("time"),
            "trigger": bool(trigger_short.get("ready")),
            "trigger_time": trigger_short.get("candle_time"),
            "chronology_valid": bool(
                bos_short
                and ret_short.get("valid")
                and int(ret_short.get("index") or -1) > int(bos_short.get("index") or -1)
                and int(trigger_short.get("candle_time") or 0) > int(ret_short.get("time") or 0)
            ),
        },
    }

    reasons: list[str] = []
    if regime4["bull"]:
        reasons.append("4H bullish regime")
    if regime4["bear"]:
        reasons.append("4H bearish regime")
    if len(c1d) >= 20:
        daily_structure = get_structure(c1d)
        if daily_structure != "UNKNOWN":
            reasons.append(f"1D structure {daily_structure}")
    if long1:
        reasons.append("1H bullish alignment")
    if short1:
        reasons.append("1H bearish alignment")
    if bos_long:
        reasons.append("15M LONG BOS confirmed")
    if bos_short:
        reasons.append("15M SHORT BOS confirmed")
    if ret_long.get("valid"):
        reasons.append("15M LONG post-BOS retest")
    if ret_short.get("valid"):
        reasons.append("15M SHORT post-BOS retest")
    if trigger_long.get("ready"):
        reasons.append("5M LONG trigger confirmed")
    if trigger_short.get("ready"):
        reasons.append("5M SHORT trigger confirmed")
    if momentum_ok:
        reasons.append("Momentum aligned")
    if volume_ok:
        reasons.append("Volume/RVOL aligned")
    if target_path_ok:
        reasons.append("Structural target path acceptable")
    if sl_atr_ok:
        reasons.append("SL ATR distance acceptable")
    if risk_ok:
        reasons.append("TP2 RR >= 2.0")
    if volatility_ok:
        reasons.append("Volatility gate acceptable")
    if not technical_candidate:
        reasons.append("Technical hard gate failed")

    return {
        "symbol": symbol,
        "price": price,
        "setup": setup,
        "setup_candidate": setup if setup in {"LONG", "SHORT"} else "NO TRADE",
        "trend_4h": "BULLISH" if regime4["bull"] else "BEARISH" if regime4["bear"] else "NO_TRADE",
        "regime": regime4["regime"],
        "daily_structure_1d": get_structure(c1d) if len(c1d) >= 20 else "UNAVAILABLE",
        "structure_1h": structure1,
        "protected_structure_1h": protected_1h_direction,
        "protected_high": protected1.get("protected_high"),
        "protected_low": protected1.get("protected_low"),
        "protected_1h_long_intact": bool(protected1.get("protected_low") is not None and last_close1 > float(protected1["protected_low"])),
        "protected_1h_short_intact": bool(protected1.get("protected_high") is not None and last_close1 < float(protected1["protected_high"])),
        "bos_15m": bool(active_bos),
        "bos_15m_time": (active_bos or {}).get("time"),
        "bos_15m_index": (active_bos or {}).get("index"),
        "bos_15m_strength": _num((active_bos or {}).get("strength")),
        "long_bos_level": bos_long.get("level") if bos_long else None,
        "short_bos_level": bos_short.get("level") if bos_short else None,
        "long_bos_event_count": len(long_bos_events),
        "short_bos_event_count": len(short_bos_events),
        "long_retest": bool(ret_long.get("valid")),
        "short_retest": bool(ret_short.get("valid")),
        "long_retest_time": ret_long.get("time"),
        "short_retest_time": ret_short.get("time"),
        "long_retest_failure": ret_long.get("failure"),
        "short_retest_failure": ret_short.get("failure"),
        "retest": active_retest or {},
        "ema21": ema21_15,
        "ema50": ema50_15,
        "ema21_4h": regime4["e21"],
        "ema50_4h": regime4["e50"],
        "ema100_4h": regime4["e100"],
        "ema200_4h": regime4["e200"],
        "ema21_1h": e21_1,
        "ema50_1h": e50_1,
        "ema_direction": ema_direction,
        "rsi": r15,
        "rsi_5m": trigger["rsi"],
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "atr": atr15,
        "atr_4h": regime4["atr"],
        "atr_5m": trigger["atr"],
        "atr_pct": atr_pct,
        "atr_percentile": atr_rank,
        "adx_4h": regime4["adx"],
        "ema50_slope_4h": regime4["slope"],
        "volume": vol15,
        "rvol": rv15,
        "rvol_15m": rv15,
        "rvol_5m": trigger["rvol"],
        "support": support,
        "resistance": resistance,
        "futures_context": "PENDING",
        "futures_ok": False,
        "btc_filter_ok": False,
        "btc_filter_reason": "PENDING",
        "data_fresh": True,
        "signal_engine_version": ENGINE_VERSION,
        "trigger_5m": "CONFIRMED" if trigger.get("ready") else "NONE",
        "trigger_quality_5m": trigger["quality"],
        "trigger_quality": trigger["quality"],
        "five_minute_ready": trigger["ready"],
        "five_minute_long": trigger_long["ready"],
        "five_minute_short": trigger_short["ready"],
        "closed_5m_candle_time": trigger["candle_time"],
        "score": score,
        "score_groups": groups,
        "confirmation_family_count": families,
        "bullish_points": int(regime4["bull"]) + int(structure1 == "HH/HL") + int(e21_1 is not None and e50_1 is not None and e21_1 >= e50_1),
        "bearish_points": int(regime4["bear"]) + int(structure1 == "LH/LL") + int(e21_1 is not None and e50_1 is not None and e21_1 <= e50_1),
        "direction_ok": direction_ok,
        "structure_ok": structure_ok,
        "setup_ok": setup_ok,
        "momentum_ok": momentum_ok,
        "volume_ok": volume_ok,
        "location_ok": location_ok,
        "long_location_ok": bool(long_level_flags["location_ok"]),
        "short_location_ok": bool(short_level_flags["location_ok"]),
        "volatility_ok": volatility_ok,
        "risk_ok": risk_ok,
        "long_risk_ok": bool(long_level_flags["risk_ok"]),
        "short_risk_ok": bool(short_level_flags["risk_ok"]),
        "long_sl_atr": float(long_level_flags["sl_atr"]),
        "short_sl_atr": float(short_level_flags["sl_atr"]),
        "long_rr": long_level_flags["rr"],
        "short_rr": short_level_flags["rr"],
        "long_entry_distance_atr": float(long_level_flags["entry_distance_atr"]),
        "short_entry_distance_atr": float(short_level_flags["entry_distance_atr"]),
        "sl_atr": sl_atr,
        "sl_atr_ok": sl_atr_ok,
        "entry_distance_atr": entry_distance,
        "target_path_ok": bool(levels.get("target_path_ok")),
        "long_target_path_ok": bool(long_levels.get("target_path_ok") and long_levels.get("target_path_structural")),
        "short_target_path_ok": bool(short_levels.get("target_path_ok") and short_levels.get("target_path_structural")),
        "long_target_path_reason": long_levels.get("target_path_reason"),
        "short_target_path_reason": short_levels.get("target_path_reason"),
        "target_path_structural": bool(levels.get("target_path_structural")),
        "target_path_reason": levels.get("target_path_reason"),
        "target_obstacle": levels.get("target_obstacle"),
        "technical_candidate": technical_candidate,
        "signal_blocked": signal_blocked,
        "rejection_stage": None if technical_candidate else "TECHNICAL",
        "technical_gate_failures": technical_failures,
        "technical_primary_failure": primary_failure,
        "technical_gate_failure_count": len(technical_failures),
        "long_gate_status": long_gates,
        "short_gate_status": short_gates,
        "four_h_gate_components": regime4["components"],
        "four_h_failed_components": four_h_failures,
        "one_h_gate_components": {"LONG": long_1h_components, "SHORT": short_1h_components},
        "one_h_failed_components": one_h_failures,
        "volatility_gate_components": volatility_components,
        "momentum_gate_components": momentum_components,
        "momentum_long_ok": momentum_long_ok,
        "momentum_short_ok": momentum_short_ok,
        "volume_gate_components": volume_components,
        "volume_long_ok": volume_long_ok,
        "volume_short_ok": volume_short_ok,
        "trigger_5m_long_diagnostics": trigger_long,
        "trigger_5m_short_diagnostics": trigger_short,
        "setup_sequence": sequence,
        "bos_retest_trigger_sequence_valid": bool(
            sequence["LONG"]["chronology_valid"] or sequence["SHORT"]["chronology_valid"]
        ),
        "setup_fresh": setup_fresh,
        "setup_fresh_reason": setup_fresh_reason,
        "setup_bos_time": (active_bos or {}).get("time"),
        "setup_retest_time": (active_retest or {}).get("time"),
        "reasons": reasons,
        "candle_time": int(c15[-1]["time"]),
        "data_quality": quality_results,
        "regime_4h_diagnostics": regime4,
        "min_score": MIN_SCORE,
        "min_rr": MIN_RR,
        "min_families": MIN_FAMILIES,
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
