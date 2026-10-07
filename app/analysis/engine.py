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

BTC_SHOCK_ATR = 2.0
DEFAULT_MAX_HOLD_MINUTES = 72 * 60

# V11 strategy controls. These are the only strategy thresholds used by the
# authoritative 1D -> 12H -> 4H -> 1H engine.
V11_MIN_RR = 1.60
V11_MIN_IMPULSE_ATR = 2.50
V11_SETUP_MAX_4H_BARS = 30
V11_MAX_TRIGGER_BARS = 6
V11_STOP_BUFFER_ATR_4H = 0.20
V11_STOP_BUFFER_ATR_1H = 0.15
V11_MIN_SL_ATR_SANITY = 0.20
V11_MAX_SL_ATR_SANITY = 3.00
V11_SHOCK_RANGE_ATR = 4.50
V11_TRIGGER_MIN_BODY_RATIO = 0.25
V11_TRIGGER_MIN_CLOSE_LOCATION = 0.58
V11_TARGET_LEVEL_TOLERANCE_ATR = 0.10
V11_ENGINE_RR_EPSILON = 1e-9
ENGINE_VERSION = "V11-value-pullback-liquidity-reclaim-fixed"


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



# ============================================================
# V11 — TREND PULLBACK / VALUE RE-ENTRY / LIQUIDITY SWEEP
# Authoritative timeframes: 1D / 12H / 4H / 1H only.
# ============================================================

CONFIRMATION_FAMILY_NAMES = (
    "trend_context",
    "value_location",
    "liquidity_reclaim",
    "relative_volume",
    "volatility",
    "target_geometry",
)


def _data_quality(candles: list[Candle], timeframe: str, minimum: int) -> tuple[bool, str]:
    if len(candles) < minimum:
        return False, f"Insufficient {timeframe} candles: {len(candles)}<{minimum}"
    times = [int(c["time"]) for c in candles]
    if times != sorted(set(times)):
        return False, f"{timeframe} candle timestamps are not strictly increasing"
    return True, "OK"


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
    closes = [float(c["close"]) for c in candles]
    e21 = _safe_ema(closes, 21)
    e50 = _safe_ema(closes, 50)
    e200 = _safe_ema(closes, 200)
    slope = _ema_slope(closes, 50, lookback=5)
    structure = _v11_structure(candles, 3, 3)
    price = closes[-1]
    bull = bool(e50 and e200 and price > e200 and e50 > e200 and slope > 0 and structure == "HH/HL")
    bear = bool(e50 and e200 and price < e200 and e50 < e200 and slope < 0 and structure == "LH/LL")
    return {
        "regime": "BULLISH" if bull else "BEARISH" if bear else "NEUTRAL",
        "bull": bull,
        "bear": bear,
        "e21": e21,
        "e50": e50,
        "e200": e200,
        "slope": slope,
        "structure": structure,
        "price": price,
    }


def _v11_context_12h(candles: list[Candle], side: str) -> dict[str, Any]:
    closes = [float(c["close"]) for c in candles]
    e21 = _safe_ema(closes, 21)
    e50 = _safe_ema(closes, 50)
    slope = _ema_slope(closes, 50, lookback=5)
    structure = _v11_structure(candles, 3, 3)
    if e21 is None or e50 is None:
        return {"status": "NEUTRAL", "hostile": False, "healthy": False, "structure": structure, "e21": e21, "e50": e50, "slope": slope, "price": closes[-1] if closes else 0.0}
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


def _v11_find_impulses(candles: list[Candle], side: str, max_age: int = V11_SETUP_MAX_4H_BARS) -> list[dict[str, Any]]:
    """Find completed 4H continuation impulses with strict HH/HL or LH/LL sequencing.

    LONG sequence: confirmed lower-low reference -> prior high -> higher-low -> higher-high.
    SHORT sequence: confirmed higher-high reference -> prior low -> lower-high -> lower-low.
    Only centered pivots that are fully confirmed by their right-side candles are used.
    """
    if side not in {"LONG", "SHORT"} or len(candles) < 40:
        return []
    highs, lows = _swing_points(candles, left=3, right=3)
    atrs = _atr_series(candles, 14)
    out: list[dict[str, Any]] = []

    if side == "LONG":
        for hi_pos in range(len(highs)):
            hi_idx, hi_price = highs[hi_pos]
            if len(candles) - 1 - hi_idx > max_age:
                continue
            lows_before = [x for x in lows if x[0] < hi_idx]
            if not lows_before:
                continue
            lo_idx, lo_price = lows_before[-1]
            highs_before_lo = [x for x in highs[:hi_pos] if x[0] < lo_idx]
            if not highs_before_lo:
                continue
            prior_hi_idx, prior_hi_price = highs_before_lo[-1]
            lows_before_prior_hi = [x for x in lows if x[0] < prior_hi_idx]
            if not lows_before_prior_hi:
                continue
            prior_lo_idx, prior_lo_price = lows_before_prior_hi[-1]
            if not (prior_lo_idx < prior_hi_idx < lo_idx < hi_idx):
                continue
            if not (lo_price > prior_lo_price and hi_price > prior_hi_price):
                continue
            atr4 = _num(atrs[hi_idx] if hi_idx < len(atrs) else 0.0)
            leg = hi_price - lo_price
            if atr4 <= 0 or leg < V11_MIN_IMPULSE_ATR * atr4:
                continue
            out.append({
                "side": side,
                "low_idx": lo_idx,
                "high_idx": hi_idx,
                "low": float(lo_price),
                "high": float(hi_price),
                "prior_low": float(prior_lo_price),
                "prior_high": float(prior_hi_price),
                "prior_low_idx": prior_lo_idx,
                "prior_high_idx": prior_hi_idx,
                "atr": atr4,
                "leg": leg,
                "leg_atr": leg / atr4,
                "high_time": int(candles[hi_idx]["time"]),
                "low_time": int(candles[lo_idx]["time"]),
            })
    else:
        for lo_pos in range(len(lows)):
            lo_idx, lo_price = lows[lo_pos]
            if len(candles) - 1 - lo_idx > max_age:
                continue
            highs_before = [x for x in highs if x[0] < lo_idx]
            if not highs_before:
                continue
            hi_idx, hi_price = highs_before[-1]
            lows_before_hi = [x for x in lows[:lo_pos] if x[0] < hi_idx]
            if not lows_before_hi:
                continue
            prior_lo_idx, prior_lo_price = lows_before_hi[-1]
            highs_before_prior_lo = [x for x in highs if x[0] < prior_lo_idx]
            if not highs_before_prior_lo:
                continue
            prior_hi_idx, prior_hi_price = highs_before_prior_lo[-1]
            if not (prior_hi_idx < prior_lo_idx < hi_idx < lo_idx):
                continue
            if not (hi_price < prior_hi_price and lo_price < prior_lo_price):
                continue
            atr4 = _num(atrs[lo_idx] if lo_idx < len(atrs) else 0.0)
            leg = hi_price - lo_price
            if atr4 <= 0 or leg < V11_MIN_IMPULSE_ATR * atr4:
                continue
            out.append({
                "side": side,
                "low_idx": lo_idx,
                "high_idx": hi_idx,
                "low": float(lo_price),
                "high": float(hi_price),
                "prior_low": float(prior_lo_price),
                "prior_high": float(prior_hi_price),
                "prior_low_idx": prior_lo_idx,
                "prior_high_idx": prior_hi_idx,
                "atr": atr4,
                "leg": leg,
                "leg_atr": leg / atr4,
                "high_time": int(candles[hi_idx]["time"]),
                "low_time": int(candles[lo_idx]["time"]),
            })

    unique: dict[tuple[int, int, str], dict[str, Any]] = {}
    for item in out:
        key = (int(item["low_idx"]), int(item["high_idx"]), side)
        unique[key] = item
    return sorted(unique.values(), key=lambda x: int(x["high_idx"] if side == "LONG" else x["low_idx"]))


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
        deep_low = high - 0.786 * rng
        deep_high = high - 0.50 * rng
    else:
        retrace_low = low + 0.382 * rng
        retrace_high = low + 0.786 * rng
        deep_low = low + 0.50 * rng
        deep_high = low + 0.786 * rng
    if ema21 is None or ema50 is None:
        return {"ok": retrace_low < retrace_high, "low": retrace_low, "high": retrace_high, "deep_low": min(deep_low, deep_high), "deep_high": max(deep_low, deep_high), "retracement_mid": (retrace_low + retrace_high) / 2.0, "retracement_low": retrace_low, "retracement_high": retrace_high}
    corridor_low = min(ema21, ema50) - 0.15 * float(leg["atr"])
    corridor_high = max(ema21, ema50) + 0.15 * float(leg["atr"])
    zone_low = max(retrace_low, corridor_low)
    zone_high = min(retrace_high, corridor_high)
    # A no-overlap EMA corridor must not silently invent another value zone;
    # the Fibonacci pullback remains the deterministic fallback.
    if zone_low >= zone_high:
        zone_low, zone_high = retrace_low, retrace_high
    return {"ok": zone_low < zone_high, "low": zone_low, "high": zone_high, "deep_low": min(deep_low, deep_high), "deep_high": max(deep_low, deep_high), "retracement_mid": (retrace_low + retrace_high) / 2.0, "retracement_low": retrace_low, "retracement_high": retrace_high}


def _v11_value_touched(candles_1h: list[Candle], start_time: int, zone: dict[str, Any]) -> list[int]:
    zlow, zhigh = _num(zone.get("low")), _num(zone.get("high"))
    if zlow <= 0 or zhigh <= 0 or zlow >= zhigh:
        return []
    return [i for i, c in enumerate(candles_1h) if int(c["time"]) > start_time and float(c["low"]) <= zhigh and float(c["high"]) >= zlow]


def _v11_liquidity_trigger(candles: list[Candle], start_idx: int, side: str, max_bars: int = V11_MAX_TRIGGER_BARS) -> dict[str, Any]:
    """Strict two-candle liquidity event: sweep first, reclaim on a later candle."""
    empty = {
        "ready": False,
        "sweep_idx": None,
        "reclaim_idx": None,
        "swept_level": None,
        "rvol": 0.0,
        "body_ratio": 0.0,
        "close_location": 0.0,
        "quality": 0.0,
        "reason": "no strict sweep → subsequent reclaim",
    }
    if side not in {"LONG", "SHORT"} or start_idx < 3 or start_idx >= len(candles):
        return empty
    end = min(len(candles) - 1, start_idx + max_bars)
    for i in range(start_idx, end):
        prior = candles[i - 3:i]
        if len(prior) != 3:
            continue
        c = candles[i]
        if side == "LONG":
            level = min(float(x["low"]) for x in prior)
            swept = float(c["low"]) < level and float(c["close"]) <= level
        else:
            level = max(float(x["high"]) for x in prior)
            swept = float(c["high"]) > level and float(c["close"]) >= level
        if not swept:
            continue
        for j in range(i + 1, end + 1):
            r = candles[j]
            ropen, rhigh, rlow, rclose = map(float, (r["open"], r["high"], r["low"], r["close"]))
            rrng = max(rhigh - rlow, 1e-12)
            body_ratio = abs(rclose - ropen) / rrng
            raw_loc = (rclose - rlow) / rrng
            close_loc = raw_loc if side == "LONG" else 1.0 - raw_loc
            reclaimed = (rclose > level and rclose > ropen) if side == "LONG" else (rclose < level and rclose < ropen)
            if not reclaimed:
                continue
            if body_ratio + 1e-12 < V11_TRIGGER_MIN_BODY_RATIO or close_loc + 1e-12 < V11_TRIGGER_MIN_CLOSE_LOCATION:
                continue
            rvol = _relative_volume(candles[:j + 1], 20)
            quality = _clamp(
                0.45
                + 0.20 * _clamp(body_ratio / 0.50, 0, 1)
                + 0.20 * _clamp(rvol / 1.20, 0, 1)
                + 0.15 * _clamp(close_loc / 0.70, 0, 1),
                0,
                1,
            )
            return {
                "ready": True,
                "sweep_idx": i,
                "reclaim_idx": j,
                "swept_level": float(level),
                "rvol": rvol,
                "body_ratio": body_ratio,
                "close_location": close_loc,
                "quality": quality,
                "reason": "strict liquidity sweep followed by later reclaim",
                "trigger_time": int(r["time"]),
                "sweep_time": int(c["time"]),
            }
    return empty


def _fresh_structural_levels(candles: list[Candle], timeframe: str, side: str, entry: float) -> list[dict[str, Any]]:
    highs, lows = _swing_points(candles, 3, 3)
    levels: list[dict[str, Any]] = []
    if side == "LONG":
        for idx, price in highs:
            price = float(price)
            if price <= entry:
                continue
            # A resistance pivot is "fresh" until a later candle has already traded
            # through it. This is evaluated using only completed candles.
            if any(float(c["high"]) >= price for c in candles[idx + 1:]):
                continue
            levels.append({"price": price, "timeframe": timeframe, "index": int(idx), "time": int(candles[idx]["time"]), "kind": "RESISTANCE"})
    else:
        for idx, price in lows:
            price = float(price)
            if price >= entry:
                continue
            if any(float(c["low"]) <= price for c in candles[idx + 1:]):
                continue
            levels.append({"price": price, "timeframe": timeframe, "index": int(idx), "time": int(candles[idx]["time"]), "kind": "SUPPORT"})
    return levels


def _v11_target(
    c4: list[Candle],
    c12: list[Candle],
    c1d: list[Candle],
    c1: list[Candle],
    entry: float,
    side: str,
    impulse: dict[str, Any],
    atr4: float,
) -> dict[str, Any]:
    """Choose the nearest fresh HTF target, then independently validate its path.

    A farther target is never selected merely to manufacture RR. If the nearest
    structurally valid target has a real intervening structural blocker, the setup fails.
    """
    levels: list[dict[str, Any]] = []
    for candles, tf in ((c4, "4H"), (c12, "12H"), (c1d, "1D")):
        levels.extend(_fresh_structural_levels(candles, tf, side, entry))

    impulse_price = float(impulse["high"] if side == "LONG" else impulse["low"])
    impulse_time = int(impulse["high_time"] if side == "LONG" else impulse["low_time"])
    if (side == "LONG" and impulse_price > entry) or (side == "SHORT" and impulse_price < entry):
        consumed = any((float(c["high"]) >= impulse_price if side == "LONG" else float(c["low"]) <= impulse_price) for c in c4[int(impulse["high_idx"] if side == "LONG" else impulse["low_idx"]) + 1:])
        if not consumed:
            levels.append({"price": impulse_price, "timeframe": "4H_IMPULSE", "index": int(impulse["high_idx"] if side == "LONG" else impulse["low_idx"]), "time": impulse_time, "kind": "IMPULSE_TARGET"})

    if not levels:
        return {"ok": False, "structural": False, "path_clear": False, "reason": "no fresh structural target beyond entry", "target_levels": [], "blocking_levels": []}

    # Choose the nearest valid structural level on either side. A farther
    # short target must never win merely because ``entry - price`` is larger.
    target = min(levels, key=lambda x: abs(float(x["price"]) - entry))
    if side == "LONG":
        between = lambda p: entry < p < target["price"]
    else:
        between = lambda p: target["price"] < p < entry

    tolerance = max(abs(atr4) * V11_TARGET_LEVEL_TOLERANCE_ATR, abs(entry) * 0.0005)
    blockers: list[dict[str, Any]] = []
    # The higher-timeframe candidate set is already nearest-first. The real path
    # test therefore focuses on completed 1H structural levels that price must cross.
    blockers.extend(
        level
        for level in _fresh_structural_levels(c1, "1H", side, entry)
        if between(float(level["price"])) and abs(float(level["price"]) - float(target["price"])) > tolerance
    )
    # Also catch current 4H/12H/1D structural levels between entry and target,
    # even when the selected target comes from another timeframe.
    for level in levels:
        p = float(level["price"])
        if between(p) and abs(p - float(target["price"])) > tolerance:
            blockers.append(level)

    # Deduplicate blockers by approximate level/timeframe.
    dedup: dict[tuple[str, int, float], dict[str, Any]] = {}
    for item in blockers:
        dedup[(str(item["timeframe"]), int(item["time"]), round(float(item["price"]), 10))] = item
    blockers = sorted(dedup.values(), key=lambda x: abs(float(x["price"]) - entry))
    return {
        "ok": not blockers,
        "structural": True,
        "path_clear": not blockers,
        "price": float(target["price"]),
        "timeframe": str(target["timeframe"]),
        "time": int(target["time"]),
        "target": target,
        "target_levels": levels,
        "blocking_levels": blockers,
        "blocking_level": blockers[0] if blockers else None,
        "reason": "nearest fresh structural target with no intervening completed structural blocker" if not blockers else "nearest target path blocked by an intervening structural level",
    }


def _v11_score(features: dict[str, float]) -> tuple[int, dict[str, int]]:
    """Diagnostic score only. It is never an acceptance gate."""
    groups = {
        "impulse_structure": int(round(25 * _clamp(features.get("impulse", 0), 0, 1))),
        "value_location": int(round(25 * _clamp(features.get("value", 0), 0, 1))),
        "liquidity_reclaim": int(round(20 * _clamp(features.get("trigger", 0), 0, 1))),
        "target_geometry": int(round(15 * _clamp(features.get("target", 0), 0, 1))),
        "trend_context": int(round(10 * _clamp(features.get("context", 0), 0, 1))),
        "volatility": int(round(5 * _clamp(features.get("volatility", 0), 0, 1))),
    }
    return sum(groups.values()), groups


def build_btc_context(candles_1d: list, candles_12h: list | None, candles_4h: list, candles_1h: list | None = None) -> dict[str, Any]:
    try:
        c1d = _coerce_candles(candles_1d)
        c12 = _coerce_candles(candles_12h or [])
        c4 = _coerce_candles(candles_4h)
        c1 = _coerce_candles(candles_1h or [])
        daily = _v11_regime_1d(c1d)
        a4 = _safe_atr(c4)
        a1 = _safe_atr(c1) if c1 else 0.0
        close4 = float(c4[-1]["close"]) if c4 else 0.0
        move4 = (close4 - float(c4[-2]["close"])) / a4 if len(c4) >= 2 and a4 > 0 else 0.0
        move1 = (float(c1[-1]["close"]) - float(c1[-2]["close"])) / a1 if len(c1) >= 2 and a1 > 0 else 0.0
        return {
            "ok": True,
            "bull_1d": bool(daily.get("bull")),
            "bear_1d": bool(daily.get("bear")),
            "structure_4h": _v11_structure(c4, 3, 3),
            "bull_4h": _v11_structure(c4, 3, 3) == "HH/HL",
            "bear_4h": _v11_structure(c4, 3, 3) == "LH/LL",
            "move_4h_atr": move4,
            "move_1h_atr": move1,
            "candle_time_4h": int(c4[-1]["time"]) if c4 else 0,
            "candle_time_1h": int(c1[-1]["time"]) if c1 else 0,
            "candle_time_12h": int(c12[-1]["time"]) if c12 else 0,
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
    context = _v11_context_12h(c12, side)
    direction_ok = bool(daily.get("bull")) if side == "LONG" else bool(daily.get("bear"))
    if not direction_ok or context.get("hostile"):
        return {"side": side, "direction_ok": direction_ok, "context": context, "candidate": False, "reason": "macro direction or hostile 12H context"}

    e21_4 = _safe_ema([float(c["close"]) for c in c4], 21)
    e50_4 = _safe_ema([float(c["close"]) for c in c4], 50)
    impulses = _v11_find_impulses(c4, side)
    atr_rank = _atr_percentile(c4)
    btc_ok, btc_reason = btc_filter_ok(side, btc_context or {}, is_btc=symbol.upper().startswith("BTC"))
    if not btc_ok:
        return {"side": side, "direction_ok": direction_ok, "context": context, "candidate": False, "reason": btc_reason, "btc_filter_ok": False, "btc_filter_reason": btc_reason}

    for impulse in reversed(impulses):
        extreme_idx = int(impulse["high_idx"] if side == "LONG" else impulse["low_idx"])
        if len(c4) - 1 - extreme_idx > V11_SETUP_MAX_4H_BARS:
            continue
        # The completed 4H continuation structure must remain intact.
        if side == "LONG":
            if any(float(c["close"]) < float(impulse["low"]) for c in c4[extreme_idx + 1:]):
                continue
        else:
            if any(float(c["close"]) > float(impulse["high"]) for c in c4[extreme_idx + 1:]):
                continue

        zone = _v11_value_zone(impulse, e21_4, e50_4)
        if not zone.get("ok"):
            continue
        start_time = int(c4[extreme_idx]["time"])
        touched = _v11_value_touched(c1, start_time, zone)
        if not touched:
            continue

        for touch_idx in touched:
            trigger = _v11_liquidity_trigger(c1, touch_idx, side)
            if not trigger.get("ready") or int(trigger.get("reclaim_idx", -1)) != len(c1) - 1:
                continue

            entry = float(c1[-1]["close"])
            atr4 = max(_safe_atr(c4), float(impulse["atr"]))
            atr1 = _safe_atr(c1)
            if atr4 <= 0 or atr1 <= 0:
                continue
            sweep_start = int(trigger["sweep_idx"])
            reclaim_idx = int(trigger["reclaim_idx"])
            sweep_low = min(float(c["low"]) for c in c1[sweep_start:reclaim_idx + 1])
            sweep_high = max(float(c["high"]) for c in c1[sweep_start:reclaim_idx + 1])
            buffer = max(V11_STOP_BUFFER_ATR_4H * atr4, V11_STOP_BUFFER_ATR_1H * atr1)
            stop = sweep_low - buffer if side == "LONG" else sweep_high + buffer
            risk = abs(entry - stop)
            if risk <= 0 or not math.isfinite(risk):
                continue
            sl_atr = risk / atr4
            if sl_atr < V11_MIN_SL_ATR_SANITY or sl_atr > V11_MAX_SL_ATR_SANITY:
                continue

            target = _v11_target(c4, c12, c1d, c1, entry, side, impulse, atr4)
            if not target.get("ok"):
                continue
            tp = float(target["price"])
            if side == "LONG" and not (stop < entry < tp):
                continue
            if side == "SHORT" and not (tp < entry < stop):
                continue
            rr_gross = abs(tp - entry) / risk
            cost_price = entry * max(0.0, cost_pct)
            rr_net = (abs(tp - entry) - cost_price) / (risk + cost_price) if risk + cost_price > 0 else 0.0
            if rr_net + V11_ENGINE_RR_EPSILON < V11_MIN_RR:
                continue

            last = c1[-1]
            range1 = float(last["high"]) - float(last["low"])
            shock_ok = not (atr1 > 0 and range1 > V11_SHOCK_RANGE_ATR * atr1)
            if not shock_ok:
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
                "impulse": impulse_quality,
                "value": max(value_quality, 0.85 if deep else 0.0),
                "trigger": trigger_quality,
                "target": target_quality,
                "context": context_quality,
                "volatility": volatility_quality,
            })
            setup_identity = f"{side}|IMP:{int(impulse['low_time'])}:{int(impulse['high_time'])}|SWEEP:{int(trigger['sweep_time'])}|RECLAIM:{int(trigger['trigger_time'])}|LEVEL:{float(trigger['swept_level']):.12g}"
            return {
                "side": side,
                "candidate": True,
                "direction_ok": True,
                "context": context,
                "impulse": impulse,
                "value_zone": zone,
                "trigger": trigger,
                "entry": entry,
                "signal_reference_entry": entry,
                "stop_loss": stop,
                "tp": tp,
                "rr": rr_net,
                "rr_gross": rr_gross,
                "atr_4h": atr4,
                "atr_1h": atr1,
                "sl_atr": sl_atr,
                "tp_distance_atr": abs(tp - entry) / atr4,
                "target": target,
                "target_path_ok": bool(target.get("ok")),
                "target_path_structural": bool(target.get("structural")),
                "target_path_clear": bool(target.get("path_clear")),
                "target_path_reason": target.get("reason"),
                "blocking_levels": target.get("blocking_levels") or [],
                "blocking_level": target.get("blocking_level"),
                "score": score,
                "score_groups": score_groups,
                "atr_percentile": atr_rank,
                "btc_filter_ok": True,
                "btc_filter_reason": btc_reason,
                "value_deep": deep,
                "value_quality": value_quality,
                "shock_ok": shock_ok,
                "setup_state": "TRIGGERED",
                "entry_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
                "setup_identity": setup_identity,
            }
    return {"side": side, "direction_ok": direction_ok, "context": context, "candidate": False, "reason": "no active value-pullback setup"}


def _v11_empty(
    symbol: str,
    c1d: list[Candle],
    c12: list[Candle],
    c4: list[Candle],
    c1: list[Candle],
    reason: str,
    daily: dict[str, Any],
    btc_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    price = float(c1[-1]["close"])
    close_time = int(c1[-1]["time"]) + TIMEFRAME_MS["1h"]
    return {
        "symbol": symbol.upper(),
        "price": price,
        "setup": "NONE",
        "setup_candidate": "NONE",
        "regime_1d": daily.get("regime"),
        "daily_structure_1d": daily.get("structure"),
        "signal_engine_version": ENGINE_VERSION,
        "signal_basis": "1D trend → 12H health → 4H impulse/pullback into value → 1H strict liquidity sweep/reclaim → next 1H market execution",
        "primary_entry_timeframe": "1H",
        "setup_timeframe": "4H",
        "signal_candle_timeframe": "1H",
        "entry_mode": "MARKET",
        "entry_1h_ready": False,
        "direction_ok": False,
        "structure_ok": False,
        "setup_ok": False,
        "confirmation_ok": False,
        "location_ok": False,
        "target_path_ok": False,
        "target_path_structural": False,
        "target_path_clear": False,
        "structure_quality_ok": False,
        "trade_geometry_ok": False,
        "shock_veto_ok": False,
        "technical_candidate": False,
        "risk_ok": False,
        "rr_ok": False,
        "volatility_ok": False,
        "confirmation_family_diversity_ok": True,
        "score": 0,
        "score_groups": {},
        "confirmation_families": {},
        "confirmation_families_passed": 0,
        "confirmation_families_available": len(CONFIRMATION_FAMILY_NAMES),
        "reasons": [reason],
        "diagnostic_failures": [reason],
        "technical_gate_failures": [reason],
        "rejection_stage": "DIRECTION" if not daily.get("bull") and not daily.get("bear") else "SETUP",
        "candle_open_time": int(c1[-1]["time"]),
        "candle_close_time": close_time,
        "candle_time": close_time,
        "closed_1d_candles": len(c1d),
        "closed_12h_candles": len(c12),
        "closed_4h_candles": len(c4),
        "closed_1h_candles": len(c1),
        "btc_filter_ok": True if not btc_context else bool(btc_context.get("ok", True)),
        "btc_filter_reason": "No actionable setup",
        "btc_context": btc_context or {"ok": False},
    }


def _v11_diagnostic_failures(data: dict[str, Any]) -> list[str]:
    checks = [
        (not bool(data.get("direction_ok")), "1D directional permission failed"),
        (not bool(data.get("structure_ok")), "4H continuation structure failed"),
        (not bool(data.get("setup_ok")), "4H value pullback failed"),
        (not bool(data.get("confirmation_ok")), "1H strict sweep/reclaim failed"),
        (not bool(data.get("target_path_ok")), "No valid structural target/path"),
        (not bool(data.get("risk_ok")), "Structural risk model failed"),
        (not bool(data.get("rr_ok")), f"Post-cost RR < {V11_MIN_RR:.2f}"),
        (not bool(data.get("shock_veto_ok")), "1H shock veto failed"),
        (not bool(data.get("btc_filter_ok")), "BTC regime filter failed"),
    ]
    return [label for failed, label in checks if failed]


def calculate_confluence(data: dict[str, Any]) -> dict[str, Any]:
    """Compatibility identity function; V11 score is diagnostic, not a gate."""
    return dict(data)


def _diagnostic_failures(data: dict[str, Any]) -> list[str]:
    return _v11_diagnostic_failures(data)


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
    total = weighted = 0.0
    for c in rows:
        vol = _num(c.get("volume"))
        if vol <= 0:
            continue
        typical = (_num(c.get("high")) + _num(c.get("low")) + _num(c.get("close"))) / 3.0
        total += vol
        weighted += typical * vol
    return weighted / total if total > 0 else None


def analyze_candles(
    symbol: str,
    candles_1d: list[Any],
    candles_12h: list[Any] | None,
    candles_4h: list[Any],
    candles_1h: list[Any],
    *,
    now_ms: int | None = None,
    btc_context: dict[str, Any] | None = None,
    estimated_round_trip_cost_pct: float = 0.0018,
) -> dict[str, Any]:
    """Authoritative V11 analysis using only completed 1D/12H/4H/1H candles."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    c1d = closed_candle_rows(candles_1d or [], "1D", now)
    c4 = closed_candle_rows(candles_4h or [], "4H", now)
    c1 = closed_candle_rows(candles_1h or [], "1H", now)
    c12 = closed_candle_rows(candles_12h, "12H", now) if candles_12h is not None else synthesize_12h_from_4h(c4, now_ms=now)
    for c, tf, minimum in ((c1d, "1D", 210), (c12, "12H", 60), (c4, "4H", 180), (c1, "1H", 180)):
        ok, reason = _data_quality(c, tf, minimum)
        if not ok:
            raise ValueError(f"{symbol}: {reason}")

    daily = _v11_regime_1d(c1d)
    cost_pct = max(0.0, _num(estimated_round_trip_cost_pct, 0.0018))
    candidates = []
    for side in ("LONG", "SHORT"):
        result = _v11_analyze_side(symbol, side, c1d, c12, c4, c1, daily, cost_pct, btc_context)
        if result.get("candidate"):
            candidates.append(result)
    if not candidates:
        return _v11_empty(
            symbol, c1d, c12, c4, c1,
            "1D trend permission is neutral" if daily.get("regime") == "NEUTRAL" else "no active V11 value-pullback trigger",
            daily,
            btc_context,
        )

    # Deterministic ranking when both sides qualify. Score is only a ranking aid.
    chosen = max(
        candidates,
        key=lambda x: (
            int(x.get("score", 0)),
            int(x["trigger"].get("reclaim_idx", -1)),
            int(x["impulse"]["high_idx"] if x["side"] == "LONG" else x["impulse"]["low_idx"]),
        ),
    )
    side = str(chosen["side"])
    context = chosen["context"]
    trigger = chosen["trigger"]
    impulse = chosen["impulse"]
    target = chosen["target"]
    entry = float(chosen["entry"])
    stop = float(chosen["stop_loss"])
    tp = float(chosen["tp"])
    rr = float(chosen["rr"])
    rr_gross = float(chosen["rr_gross"])
    risk = abs(entry - stop)
    geometry_ok = bool((stop < entry < tp) if side == "LONG" else (tp < entry < stop))
    target_ok = bool(chosen.get("target_path_ok"))
    target_clear = bool(chosen.get("target_path_clear"))
    shock_ok = bool(chosen.get("shock_ok"))
    btc_ok = bool(chosen.get("btc_filter_ok", True))
    rr_ok = bool(rr + V11_ENGINE_RR_EPSILON >= V11_MIN_RR)
    atr4 = float(chosen["atr_4h"])
    atr1 = float(chosen["atr_1h"])
    atr_rank = float(chosen["atr_percentile"])

    direction_ok = True
    structure_ok = True
    setup_ok = True
    confirmation_ok = True
    volatility_ok = bool(5 <= atr_rank <= 98)
    families = {
        "trend_context": "PASS" if context.get("healthy") else "SUPPORT",
        "value_location": "PASS" if chosen.get("value_deep") else "SUPPORT",
        "liquidity_reclaim": "PASS",
        "relative_volume": "PASS" if _num(trigger.get("rvol")) >= 1.20 else "SUPPORT",
        "volatility": "PASS" if volatility_ok else "SUPPORT",
        "target_geometry": "PASS" if target_ok and target_clear else "SUPPORT",
    }
    passed = sum(value == "PASS" for value in families.values())
    score = int(chosen["score"])
    stage_status = {
        "1D_REGIME": direction_ok,
        "12H_BIAS": not bool(context.get("hostile")),
        "4H_SETUP": structure_ok and setup_ok,
        "1H_TRIGGER": confirmation_ok,
        "ENTRY_DISTANCE": True,
        "VOLATILITY": volatility_ok,
        "CONFIRMATION_FAMILIES": True,
        "TARGET_PATH": target_ok and target_clear,
        "RISK": geometry_ok,
        "RR": rr_ok,
        "QUALITY": True,
        "BTC": btc_ok,
        "SHOCK": shock_ok,
    }
    stage_failures = {key: [] for key in stage_status}
    if not target_clear:
        stage_failures["TARGET_PATH"] = [str(chosen.get("target_path_reason") or "target path is blocked")]
    if not rr_ok:
        stage_failures["RR"] = [f"Post-cost RR {rr:.2f} < {V11_MIN_RR:.2f}"]
    if not geometry_ok:
        stage_failures["RISK"] = ["SL < entry < TP geometry failed for selected side"]
    technical_candidate = bool(direction_ok and structure_ok and setup_ok and confirmation_ok and target_ok and target_clear and geometry_ok and rr_ok and volatility_ok and shock_ok and btc_ok)

    reasons = [
        f"1D {daily.get('regime')} permission",
        f"4H continuation impulse {float(impulse['leg_atr']):.2f} ATR",
        f"Value zone {float(chosen['value_zone']['low']):.6g}-{float(chosen['value_zone']['high']):.6g}",
        "1H strict liquidity sweep → subsequent reclaim",
        f"Structural TP {target.get('timeframe', 'HTF')}; post-cost RR {rr:.2f}",
    ]
    analysis = {
        "symbol": symbol.upper(),
        "price": entry,
        "setup": side,
        "setup_candidate": side,
        "regime_1d": daily.get("regime"),
        "trend_4h": "HH/HL" if side == "LONG" else "LH/LL",
        "bias_12h": context.get("status"),
        "daily_structure_1d": daily.get("structure"),
        "structure_12h": context.get("structure"),
        "structure_4h": "HH/HL" if side == "LONG" else "LH/LL",
        "protected_structure_4h": "BULLISH" if side == "LONG" else "BEARISH",
        "ema21_1d": daily.get("e21"), "ema50_1d": daily.get("e50"), "ema200_1d": daily.get("e200"),
        "ema21_12h": context.get("e21"), "ema50_12h": context.get("e50"),
        "ema21_4h": _safe_ema([float(c["close"]) for c in c4], 21), "ema50_4h": _safe_ema([float(c["close"]) for c in c4], 50),
        "ema21_1h": _safe_ema([float(c["close"]) for c in c1], 21), "ema50_1h": _safe_ema([float(c["close"]) for c in c1], 50),
        "rsi": _safe_rsi([float(c["close"]) for c in c1]), "rsi_1h_entry": _safe_rsi([float(c["close"]) for c in c1]),
        "atr": atr1, "atr_1h": atr1, "atr_4h": atr4, "atr_percentile": atr_rank,
        "rvol": _relative_volume(c1, 20), "rvol_1h": float(trigger.get("rvol", 0.0)),
        "volume": volume_status(c1), "volume_status": volume_status(c1), "adx_1d": _adx(c1d), "ema50_slope_1d": daily.get("slope"),
        "estimated_round_trip_cost_pct": cost_pct,
        "effective_round_trip_cost_pct": cost_pct,
        "futures_context": {"status": "NOT_CHECKED", "execution_ok": None},
        "futures_ok": None, "futures_execution_ok": None, "data_fresh": None,
        "signal_engine_version": ENGINE_VERSION,
        "signal_basis": "1D trend → 12H health → 4H continuation impulse/pullback into value → 1H strict liquidity sweep/reclaim → next 1H market execution",
        "primary_entry_timeframe": "1H", "setup_timeframe": "4H", "signal_candle_timeframe": "1H",
        "intraday_max_hold_minutes": DEFAULT_MAX_HOLD_MINUTES,
        "trigger_side": side,
        "trigger_quality": float(trigger.get("quality", 0.0)), "trigger_quality_1h": float(trigger.get("quality", 0.0)),
        "trigger_type": "LIQUIDITY_SWEEP_THEN_RECLAIM", "trigger_reason": trigger.get("reason"),
        "trigger_close_location": float(trigger.get("close_location", 0.0)),
        "structure_quality_ok": structure_ok, "trade_geometry_ok": geometry_ok,
        "momentum_quality": 0.5, "volume_quality": _clamp(float(trigger.get("rvol", 0.0)) / 1.20, 0, 1),
        "volatility_quality": 1.0 if volatility_ok else 0.5,
        "entry_efficiency": 1.0, "twelve_h_context_quality": 1.0 if context.get("healthy") else 0.6,
        "entry_mode": "MARKET", "limit_price": None, "entry_1h_ready": True,
        "shock_veto_ok": shock_ok, "shock_veto_reason": "OK" if shock_ok else "1H trigger candle range exceeded shock threshold",
        "score": score, "score_groups": chosen["score_groups"],
        "confirmation_families": families, "confirmation_families_passed": passed, "confirmation_families_available": len(families),
        "confirmation_family_diversity_ok": True,
        "direction_ok": direction_ok, "structure_ok": structure_ok, "setup_ok": setup_ok, "confirmation_ok": confirmation_ok,
        "momentum_ok": True, "volume_ok": True, "location_ok": target_ok and target_clear, "volatility_ok": volatility_ok,
        "risk_ok": geometry_ok, "rr_ok": rr_ok, "entry_distance_ok": True,
        "stage_status": stage_status, "stage_failures": stage_failures,
        "technical_candidate": technical_candidate,
        "signal_blocked": False, "rejection_stage": None,
        "technical_gate_failures": _v11_diagnostic_failures({
            "direction_ok": direction_ok, "structure_ok": structure_ok, "setup_ok": setup_ok,
            "confirmation_ok": confirmation_ok, "target_path_ok": target_ok and target_clear,
            "risk_ok": geometry_ok, "rr_ok": rr_ok, "shock_veto_ok": shock_ok, "btc_filter_ok": btc_ok,
        }),
        "diagnostic_failures": [], "reasons": reasons,
        "entry": entry, "signal_reference_entry": entry,
        "stop_loss": stop, "tp": tp, "rr": rr, "rr_gross": rr_gross,
        "sl_atr": risk / atr4 if atr4 > 0 else 0.0,
        "sl_atr_1h": risk / atr1 if atr1 > 0 else 0.0, "sl_atr_4h": risk / atr4 if atr4 > 0 else 0.0,
        "stop_distance_pct": risk / entry if entry > 0 else 0.0,
        "tp_distance_atr": abs(tp - entry) / atr4 if atr4 > 0 else 0.0,
        "tp_distance_atr_1h": abs(tp - entry) / atr1 if atr1 > 0 else 0.0,
        "tp_distance_pct": abs(tp - entry) / entry if entry > 0 else 0.0,
        "target_path_ok": target_ok,
        "target_path_structural": bool(target.get("structural")),
        "target_path_clear": target_clear,
        "target_path_reason": target.get("reason"),
        "target_timeframe": target.get("timeframe"),
        "target_levels": target.get("target_levels") or [],
        "blocking_levels": target.get("blocking_levels") or [],
        "blocking_level": target.get("blocking_level"),
        "stop_source": "1H sweep extreme + structural volatility buffer",
        "geometry_reason": "OK" if geometry_ok else "invalid side geometry",
        "entry_limit_price": None, "limit_entry_expiry_minutes": 0,
        "candle_open_time": int(c1[-1]["time"]),
        "candle_close_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
        "candle_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
        "entry_time": int(c1[-1]["time"]) + TIMEFRAME_MS["1h"],
        "trigger_candle_open_time": trigger.get("trigger_time"),
        "setup_impulse_high_time": impulse.get("high_time"), "setup_impulse_low_time": impulse.get("low_time"),
        "setup_pullback_touch_time": int(c1[int(trigger["sweep_idx"])]["time"]),
        "sweep_time_1h": trigger.get("sweep_time"), "reclaim_time_1h": trigger.get("trigger_time"),
        "setup_identity": chosen.get("setup_identity"),
        "setup_bos_time": None, "setup_departure_time": None,
        "value_zone_low": float(chosen["value_zone"]["low"]), "value_zone_high": float(chosen["value_zone"]["high"]),
        "value_deep": bool(chosen.get("value_deep")), "swept_level_1h": float(trigger["swept_level"]),
        "rolling_vwap_12h": _rolling_vwap(c12, 48), "flow_proxy_ratio": _backtest_flow_proxy(c1, 12),
        "closed_1d_candles": len(c1d), "closed_12h_candles": len(c12), "closed_4h_candles": len(c4), "closed_1h_candles": len(c1),
        "btc_filter_ok": btc_ok, "btc_filter_reason": chosen.get("btc_filter_reason", "OK"),
        "btc_would_block": not btc_ok, "btc_risk_mode": "OBSERVE", "btc_context": btc_context or {"ok": False},
    }
    return analysis


async def analyze_symbol(market, symbol: str) -> dict[str, Any]:
    ref = await market.resolve(symbol)
    c1d = await market.ohlcv(ref, "1D", 250)
    c4 = await market.ohlcv(ref, "4H", 650)
    c1 = await market.ohlcv(ref, "1H", 250)
    c12 = synthesize_12h_from_4h(c4)
    btc = None
    if str(symbol).upper() != "BTC_USDT":
        try:
            btc_c1d = await market.ohlcv(await market.resolve("BTC_USDT"), "1D", 250)
            btc_c4 = await market.ohlcv(await market.resolve("BTC_USDT"), "4H", 650)
            btc_c1 = await market.ohlcv(await market.resolve("BTC_USDT"), "1H", 250)
            btc_c12 = synthesize_12h_from_4h(btc_c4)
            btc = build_btc_context(btc_c1d, btc_c12, btc_c4, btc_c1)
        except Exception:
            btc = None
    return analyze_candles(symbol, c1d, c12, c4, c1, btc_context=btc)
