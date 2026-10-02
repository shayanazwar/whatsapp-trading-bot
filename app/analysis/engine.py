from __future__ import annotations

"""Deterministic MEXC Futures multi-timeframe signal engine.

V1.7 - deterministic pipeline.

1D context -> 4H regime -> 1H directional evidence ->
15M BOS/retest -> supporting momentum/volume/volatility ->
structural SL -> structural TP path -> RR -> technical candidate.

This module never places orders.
"""

import math
import time
from bisect import bisect_right
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .indicators import atr, ema, rsi, volume_status

TIMEFRAME_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "8h": 28_800_000,
    "1d": 86_400_000, "1w": 604_800_000,
}
TIMEFRAME_ALIASES = {
    "1M":"1m","1MIN":"1m","5M":"5m","5MIN":"5m","15M":"15m","15MIN":"15m",
    "30M":"30m","30MIN":"30m","1H":"1h","1HR":"1h","1HOUR":"1h",
    "4H":"4h","4HR":"4h","4HOUR":"4h","8H":"8h","8HR":"8h","8HOUR":"8h",
    "1D":"1d","1DAY":"1d","1W":"1w","1WEEK":"1w",
}

MIN_SCORE = 72
MIN_RR = 2.00
# Five supporting confirmation families out of eight, while 4H/1H/15M
# remain structural prerequisites. Live-only families may abstain when data
# is unavailable; they never auto-pass or auto-fail.
MIN_CONFIRMATION_FAMILIES = 0  # legacy informational constant; never a signal gate
MIN_AVAILABLE_CONFIRMATION_FAMILIES = 0  # legacy informational constant; never a signal gate
MIN_FAMILIES = 0
MIN_SUPPORTING_FAMILIES = 0
# Intraday-swing geometry is volatility/structure based rather than percentage
# based. The stop must clear a meaningful invalidation point with an ATR floor;
# the single TP must be a real higher-timeframe structural target.
MIN_SL_ATR = 1.00
MAX_SL_ATR = 3.50
MIN_TP_ATR = 2.50
MIN_ATR_PERCENTILE = 20.0
MAX_ATR_PERCENTILE = 95.0
MAX_SETUP_AGE_15M = 32  # up to 8 hours for a causal BOS/retest window
MAX_ENTRY_DISTANCE_ATR = 3.00
BOS_BUFFER_ATR = 0.10
BTC_SHOCK_ATR = 1.50
ADX_TREND_MIN = 16.0
MIN_TRIGGER_RVOL = 1.10
MIN_TRIGGER_BODY = 0.55
RETEST_TOLERANCE_ATR = 0.35
RETEST_PENETRATION_ATR = 0.65
INTRADAY_MAX_HOLD_MINUTES = 360
ENGINE_VERSION = "gold-v5.1-structure-analysis-15m"

CONFIRMATION_FAMILY_NAMES = (
    "momentum",
    "relative_volume",
    "volatility_regime",
    "liquidity_quality",
    "funding_crowding",
    "flow_pressure",
    "htf_target_path",
    "vwap_location",
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
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeframe_ms must be an integer or timeframe string") from exc
    if value <= 0:
        raise ValueError("timeframe_ms must be positive")
    return value


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def convert_candles(rows: Iterable[Any] | None) -> List[Candle]:
    out: List[Candle] = []
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
            candle = Candle(time=ts, open=float(o), high=float(h), low=float(l),
                            close=float(c), volume=float(v or 0))
            if not all(math.isfinite(float(candle[k])) for k in ("open","high","low","close","volume")):
                continue
            if min(candle["open"], candle["high"], candle["low"], candle["close"]) <= 0:
                continue
            if candle["low"] > candle["high"]:
                continue
            out.append(candle)
        except (TypeError, ValueError, OverflowError):
            continue
    out.sort(key=lambda x: int(x["time"]))
    dedup: Dict[int, Candle] = {}
    for c in out:
        dedup[int(c["time"])] = c
    return [dedup[t] for t in sorted(dedup)]


def closed_candle_rows(candles: Iterable[Any] | None, timeframe_ms: Any,
                       now_ms: Optional[int] = None) -> List[Candle]:
    interval = _timeframe_ms(timeframe_ms)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if isinstance(candles, list) and (not candles or isinstance(candles[0], Candle)):
        source = candles
    else:
        source = convert_candles(candles)
    return [c for c in source if int(c["time"]) + interval <= now]


def _safe_ema(values: List[float], period: int) -> Optional[float]:
    try:
        x = ema(values, period)
        return float(x) if x is not None and math.isfinite(float(x)) else None
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
    for c in candles[1:]:
        h, l = float(c["high"]), float(c["low"])
        result.append(max(h - l, abs(h - previous), abs(l - previous)))
        previous = float(c["close"])
    return result


def _safe_atr(candles: List[Candle], period: int = 14) -> float:
    try:
        return max(0.0, _num(atr(candles, period)))
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
        window += trs[i] - trs[i - period]
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
        h, l = float(cur["high"]), float(cur["low"])
        ph, pl, pc = float(prev["high"]), float(prev["low"]), float(prev["close"])
        up, down = h - ph, pl - l
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    if len(trs) < period * 2:
        return 0.0
    tr_s = sum(trs[:period]) / period
    plus_s = sum(plus_dm[:period]) / period
    minus_s = sum(minus_dm[:period]) / period
    dx: List[float] = []
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


def _relative_volume(candles: List[Candle], lookback: int = 20) -> float:
    if len(candles) < lookback + 1:
        return 0.0
    current = float(candles[-1]["volume"])
    previous = candles[-lookback - 1:-1]
    average = sum(float(c["volume"]) for c in previous) / lookback
    return current / average if average > 0 else 0.0


def _atr_percentile_from_series(
    candles: List[Candle],
    values: List[float],
    period: int = 14,
    lookback: int = 100,
) -> float:
    if len(candles) < period + 10:
        return 50.0
    end = min(len(candles), len(values))
    start = max(period, end - lookback)
    ratios: List[float] = []
    for i in range(start, end):
        price, a = float(candles[i]["close"]), float(values[i])
        if price > 0 and a > 0:
            ratios.append(a / price)
    if not ratios:
        return 50.0
    current = ratios[-1]
    return 100.0 * sum(x <= current for x in ratios) / len(ratios)


def _atr_percentile(
    candles: List[Candle],
    period: int = 14,
    lookback: int = 100,
) -> float:
    return _atr_percentile_from_series(
        candles,
        _atr_series(candles, period),
        period,
        lookback,
    )


def _macd_components(values: List[float]) -> Tuple[float, float, float, float]:
    """Return MACD line, signal, histogram and normalized histogram delta."""
    fast, slow = _ema_series(values, 12), _ema_series(values, 26)
    if not fast or not slow:
        return 0.0, 0.0, 0.0, 0.0

    n = min(len(fast), len(slow))
    line_series = [fast[-n + i] - slow[-n + i] for i in range(n)]
    signal_series = _ema_series(line_series, 9)

    line = line_series[-1]
    signal = signal_series[-1] if signal_series else 0.0
    histogram = line - signal

    delta = 0.0
    if len(signal_series) >= 2:
        current = line_series[-1] - signal_series[-1]
        previous = line_series[-2] - signal_series[-2]
        scale = max(abs(current), abs(previous), 1e-12)
        delta = (current - previous) / scale

    return line, signal, histogram, delta


def _macd(values: List[float]) -> Tuple[float, float, float]:
    line, signal, histogram, _ = _macd_components(values)
    return line, signal, histogram


def _macd_histogram_delta(values: List[float]) -> float:
    """Return the one-candle normalized MACD histogram change."""
    if len(values) < 40:
        return 0.0
    return _macd_components(values)[3]


def _swing_points(candles: List[Candle], left: int = 2, right: int = 2) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Return swing highs/lows in one pass with no temporary neighbor lists."""
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    stop = len(candles) - right
    for i in range(left, stop):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        high_ok = True
        low_ok = True
        for j in range(i - left, i):
            other_high = float(candles[j]["high"])
            other_low = float(candles[j]["low"])
            if high <= other_high:
                high_ok = False
            if low >= other_low:
                low_ok = False
            if not high_ok and not low_ok:
                break
        if high_ok:
            for j in range(i + 1, i + right + 1):
                if high <= float(candles[j]["high"]):
                    high_ok = False
                    break
        if low_ok:
            for j in range(i + 1, i + right + 1):
                if low >= float(candles[j]["low"]):
                    low_ok = False
                    break
        if high_ok:
            highs.append((i, high))
        if low_ok:
            lows.append((i, low))
    return highs, lows


def _structure_from_swings(highs: List[Tuple[int, float]], lows: List[Tuple[int, float]]) -> str:
    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"
    ph, lh = highs[-2][1], highs[-1][1]
    pl, ll = lows[-2][1], lows[-1][1]
    if lh > ph and ll > pl:
        return "HH/HL"
    if lh < ph and ll < pl:
        return "LH/LL"
    return "RANGE"


def _support_resistance_from_swings(
    candles: List[Candle],
    highs: List[Tuple[int, float]],
    lows: List[Tuple[int, float]],
) -> Tuple[Optional[float], Optional[float]]:
    if not candles:
        return None, None
    current = float(candles[-1]["close"])
    support = max((price for _, price in lows if price < current), default=None)
    resistance = min((price for _, price in highs if price > current), default=None)
    return support, resistance


def _swing_highs(candles: List[Candle], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    result = []
    for i in range(left, len(candles) - right):
        h = float(candles[i]["high"])
        if all(h > float(candles[j]["high"]) for j in range(i-left, i)) and \
           all(h > float(candles[j]["high"]) for j in range(i+1, i+right+1)):
            result.append((i, h))
    return result


def _swing_lows(candles: List[Candle], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    result = []
    for i in range(left, len(candles) - right):
        l = float(candles[i]["low"])
        if all(l < float(candles[j]["low"]) for j in range(i-left, i)) and \
           all(l < float(candles[j]["low"]) for j in range(i+1, i+right+1)):
            result.append((i, l))
    return result


def _protected_structure(
    candles: List[Candle],
    swings: Optional[Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]] = None,
) -> Dict[str, Any]:
    highs, lows = swings if swings is not None else _swing_points(candles)
    ph = highs[-1][1] if highs else None
    pl = lows[-1][1] if lows else None
    if len(highs) < 2 or len(lows) < 2:
        return {"state":"NEUTRAL","protected_high":ph,"protected_low":pl}
    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]
    state = "BULLISH" if h2 > h1 and l2 > l1 else "BEARISH" if h2 < h1 and l2 < l1 else "NEUTRAL"
    return {"state":state,"protected_high":ph,"protected_low":pl}


def _recent_swing_direction(
    candles: List[Candle],
    lookback: int = 60,
    swings: Optional[Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]] = None,
) -> Dict[str, Any]:
    sample = candles[-lookback:] if len(candles) > lookback else candles
    highs, lows = swings if swings is not None else _swing_points(sample)
    result = {"bull_higher_high":False,"bull_higher_low":False,
              "bear_lower_high":False,"bear_lower_low":False,
              "bull_score":0,"bear_score":0}
    if len(highs) >= 2:
        result["bull_higher_high"] = highs[-1][1] > highs[-2][1]
        result["bear_lower_high"] = highs[-1][1] < highs[-2][1]
    if len(lows) >= 2:
        result["bull_higher_low"] = lows[-1][1] > lows[-2][1]
        result["bear_lower_low"] = lows[-1][1] < lows[-2][1]
    result["bull_score"] = int(result["bull_higher_high"]) + int(result["bull_higher_low"])
    result["bear_score"] = int(result["bear_lower_high"]) + int(result["bear_lower_low"])
    return result


def _four_hour_regime(candles: List[Candle]) -> Dict[str, Any]:
    close = [float(c["close"]) for c in candles]
    e21, e50, e100, e200 = (_safe_ema(close, p) for p in (21,50,100,200))
    a, adx, slope = _safe_atr(candles), _adx(candles), _ema_slope(close, 50)
    swings = _swing_points(candles)
    protected = _protected_structure(candles, swings)
    recent_sample = candles[-80:] if len(candles) > 80 else candles
    recent_swings = _swing_points(recent_sample)
    recent = _recent_swing_direction(recent_sample, len(recent_sample), recent_swings)
    base = {"bull":False,"bear":False,"regime":"NO_TRADE","e21":e21,"e50":e50,
            "e100":e100,"e200":e200,"atr":a,"adx":adx,"slope":slope,
            "protected":protected,"swings":recent,"bull_votes":0,"bear_votes":0}
    if None in (e21,e50,e100,e200) or not close:
        return base
    current = close[-1]
    bull_votes = sum((current > e200, e21 >= e50, e50 >= e100,
                      slope > 0.0, protected["state"] == "BULLISH",
                      recent["bull_score"] >= 1))
    bear_votes = sum((current < e200, e21 <= e50, e50 <= e100,
                      slope < 0.0, protected["state"] == "BEARISH",
                      recent["bear_score"] >= 1))
    bull = bool(current > e200 and e21 >= e50 and adx >= ADX_TREND_MIN and
                bull_votes >= 4 and bull_votes > bear_votes)
    bear = bool(current < e200 and e21 <= e50 and adx >= ADX_TREND_MIN and
                bear_votes >= 4 and bear_votes > bull_votes)
    regime = "BULLISH" if bull else "BEARISH" if bear else "SIDEWAYS"
    base.update({"bull":bull,"bear":bear,"regime":regime,
                 "bull_votes":bull_votes,"bear_votes":bear_votes})
    return base


def _one_hour_alignment(candles: List[Candle], regime4: Dict[str, Any]) -> Dict[str, Any]:
    close = [float(c["close"]) for c in candles]
    price = close[-1]
    e21, e50, e200 = _safe_ema(close,21), _safe_ema(close,50), _safe_ema(close,200)
    full_swings = _swing_points(candles)
    structure = _structure_from_swings(*full_swings)
    protected = _protected_structure(candles, full_swings)
    recent_sample = candles[-70:] if len(candles) > 70 else candles
    recent = _recent_swing_direction(recent_sample, len(recent_sample))
    slope, r, a = _ema_slope(close,50), _safe_rsi(close), _safe_atr(candles)
    base = {"long":False,"short":False,"structure":structure,"protected":protected,"swings":recent,
            "e21":e21,"e50":e50,"e200":e200,"slope":slope,"rsi":r,"atr":a,
            "long_votes":0,"short_votes":0}
    if e21 is None or e50 is None:
        return base
    tolerance = max(a * 0.25, 1e-12)
    long_ema = price >= e50 - tolerance and e21 >= e50
    short_ema = price <= e50 + tolerance and e21 <= e50
    long_structure = structure == "HH/HL" or protected["state"] == "BULLISH" or recent["bull_score"] >= 1
    short_structure = structure == "LH/LL" or protected["state"] == "BEARISH" or recent["bear_score"] >= 1
    long_momentum = r >= 50.0 and (e200 is None or price >= e200 - max(a * 0.50, 1e-12))
    short_momentum = r <= 50.0 and (e200 is None or price <= e200 + max(a * 0.50, 1e-12))
    long_slope = slope >= 0.0
    short_slope = slope <= 0.0
    lv, sv = sum((long_ema,long_structure,long_momentum,long_slope)), sum((short_ema,short_structure,short_momentum,short_slope))
    # 1H decides direction. 4H is a regime/context filter handled separately,
    # which lets a clear 1H trend trade through a sideways 4H regime.
    long = bool(lv >= 3 and lv > sv)
    short = bool(sv >= 3 and sv > lv)
    base.update({"long":long,"short":short,"long_votes":lv,"short_votes":sv,
                 "long_ema":long_ema,"short_ema":short_ema,
                 "long_structure":long_structure,"short_structure":short_structure,
                 "long_momentum":long_momentum,"short_momentum":short_momentum,
                 "long_slope":long_slope,"short_slope":short_slope})
    return base


def _bos_strength(candle: Candle, level: float, atr_value: float) -> float:
    rng = max(float(candle["high"]) - float(candle["low"]), 1e-12)
    body = abs(float(candle["close"]) - float(candle["open"])) / rng
    displacement = abs(float(candle["close"]) - level) / atr_value if atr_value > 0 else 0.0
    return _clamp(0.5*_clamp(body/0.55,0,1)+0.5*_clamp(displacement/0.50,0,1),0,1)


def _bos_events(
    candles: List[Candle],
    side: str,
    lookback: int = 70,
    *,
    atr_values: Optional[List[float]] = None,
    swings: Optional[Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]] = None,
    progress_callback: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Find BOS events without rebuilding the pivot list inside every candle.

    The event semantics are unchanged: the latest confirmed swing at each
    candle is tested first, the swing must satisfy ``idx + 2 <= i``, and the
    ATR/buffer are taken from the event candle.
    """
    if len(candles) < 10 or side not in {"LONG", "SHORT"}:
        return []

    atr_values = atr_values if atr_values is not None else _atr_series(candles, 14)
    swings = swings if swings is not None else _swing_points(candles)
    swing_highs, swing_lows = swings
    pivots = swing_highs if side == "LONG" else swing_lows
    pivot_indices = [idx for idx, _ in pivots]

    events: List[Dict[str, Any]] = []
    start = max(1, len(candles) - lookback)

    # Pivots are already sorted by index. For each event candle, binary-search
    # the last pivot that is confirmed by that candle, instead of rebuilding
    # ``[(idx, p) for ... if ...]`` on every iteration.
    for loop_offset, i in enumerate(range(start, len(candles)), start=1):
        if progress_callback is not None and (
            loop_offset == 1 or loop_offset % 250 == 0
        ):
            try:
                progress_callback(
                    "BOS_PROGRESS",
                    {
                        "side": side,
                        "processed": loop_offset,
                        "total": max(0, len(candles) - start),
                    },
                )
            except Exception:
                pass

        a = float(atr_values[i]) if i < len(atr_values) else 0.0
        if a <= 0:
            continue

        close = float(candles[i]["close"])
        prev = float(candles[i - 1]["close"])
        buffer = max(a * BOS_BUFFER_ATR, 1e-12)
        last_eligible_pos = bisect_right(pivot_indices, i - 2) - 1
        if last_eligible_pos < 0:
            continue

        for pos in range(last_eligible_pos, -1, -1):
            swing_index, raw_level = pivots[pos]
            level = float(raw_level)
            if side == "LONG":
                crossed = prev <= level + buffer and close > level + buffer
            else:
                crossed = prev >= level - buffer and close < level - buffer

            if crossed:
                events.append({
                    "index": i,
                    "time": int(candles[i]["time"]),
                    "level": level,
                    "atr": a,
                    "strength": _bos_strength(candles[i], level, a),
                    "swing_index": swing_index,
                })
                break

    return events


def _build_15m_backtest_context(
    candles: List[Candle],
    progress_callback: Optional[Any] = None,
) -> Dict[str, Any]:
    """Precompute reusable 15M structures for point-in-time backtests.

    Every stored value is derived from the complete historical array but is
    consumed only up to the candidate prefix, so causal boundaries remain
    identical to the ordinary per-prefix engine path.

    ``progress_callback`` is diagnostic-only. It never changes the calculations
    or trading decisions and allows the isolated backtest worker to report which
    CPU stage is currently active.
    """
    def report(stage: str, detail: Any = None) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(stage, detail)
        except Exception:
            pass

    report("CONTEXT_ATR_START", {"candles": len(candles)})
    atr_values = _atr_series(candles, 14)
    report("CONTEXT_ATR_DONE", {"candles": len(candles)})

    report("CONTEXT_SWINGS_START", {"candles": len(candles)})
    swings = _swing_points(candles)
    report(
        "CONTEXT_SWINGS_DONE",
        {"highs": len(swings[0]), "lows": len(swings[1])},
    )

    report("CONTEXT_BOS_LONG_START", {"candles": len(candles)})
    bos_long = _bos_events(
        candles,
        "LONG",
        lookback=len(candles),
        atr_values=atr_values,
        swings=swings,
        progress_callback=progress_callback,
    )
    report("CONTEXT_BOS_LONG_DONE", {"events": len(bos_long)})

    report("CONTEXT_BOS_SHORT_START", {"candles": len(candles)})
    bos_short = _bos_events(
        candles,
        "SHORT",
        lookback=len(candles),
        atr_values=atr_values,
        swings=swings,
        progress_callback=progress_callback,
    )
    report("CONTEXT_BOS_SHORT_DONE", {"events": len(bos_short)})
    return {
        "count": len(candles),
        "last_time": int(candles[-1]["time"]) if candles else 0,
        "last_close": float(candles[-1]["close"]) if candles else 0.0,
        "atr": atr_values,
        "swings": swings,
        "bos_long": bos_long,
        "bos_short": bos_short,
        # Precomputed indexes let every point-in-time prefix slice BOS events with
        # two binary searches instead of scanning the entire event list.
        "bos_long_indices": [int(event["index"]) for event in bos_long],
        "bos_short_indices": [int(event["index"]) for event in bos_short],
    }


def _slice_backtest_bos_events(
    context: Dict[str, Any],
    prefix_count: int,
    lookback: int = 70,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return exactly the BOS window visible to a candidate prefix."""
    start = max(1, int(prefix_count) - int(lookback))

    def _slice(events_key: str, indices_key: str) -> List[Dict[str, Any]]:
        events = context.get(events_key, []) or []
        indices = context.get(indices_key)
        if not indices:
            # Compatibility path for externally supplied contexts created by an
            # older engine version. The authoritative event semantics are unchanged.
            return [
                event for event in events
                if start <= int(event.get("index", -1)) < prefix_count
            ]
        left = bisect_right(indices, start - 1)
        right = bisect_right(indices, prefix_count - 1)
        return list(events[left:right])

    return (
        _slice("bos_long", "bos_long_indices"),
        _slice("bos_short", "bos_short_indices"),
    )


def _pullback_retest(candles: List[Candle], side: str, bos: Optional[Dict[str,Any]], max_bars: int=8) -> Dict[str,Any]:
    invalid={"valid":False,"index":None,"time":None,"level":bos.get("level") if bos else None,
             "quality":0.0,"rejection":False,"low":None,"high":None}
    if not bos: return invalid
    start, end = int(bos["index"])+1, min(len(candles), int(bos["index"])+1+max_bars)
    if start >= end: return invalid
    level, a = float(bos["level"]), max(_num(bos.get("atr")),0)
    if a <= 0: return invalid
    tol = max(a * RETEST_TOLERANCE_ATR, 1e-12)
    penetration = max(a * RETEST_PENETRATION_ATR, 1e-12)
    close_tol = max(a * 0.20, 1e-12)
    for i in range(start,end):
        c=o=None
        o,h,l,close = float(candles[i]["open"]),float(candles[i]["high"]),float(candles[i]["low"]),float(candles[i]["close"])
        rng=max(h-l,1e-12)
        if side=="LONG":
            intersects=l<=level+tol and h>=level-penetration
            held=close>=level-close_tol
            wick=min(o,close)-l
        else:
            intersects=h>=level-tol and l<=level+penetration
            held=close<=level+close_tol
            wick=h-max(o,close)
        if not (intersects and held): continue
        rejection=wick/rng>=0.18
        quality=0.70 + (0.20 if rejection else 0) + (0.10 if (side=="LONG" and close>o) or (side=="SHORT" and close<o) else 0)
        return {"valid":True,"index":i,"time":int(candles[i]["time"]),"level":level,
                "quality":_clamp(quality,0,1),"rejection":rejection,"low":l,"high":h}
    return invalid


def _select_latest_bos_with_retest(candles: List[Candle], side: str, events: Optional[List[Dict[str, Any]]] = None):
    events = events if events is not None else _bos_events(candles, side)
    latest = len(candles)-1
    for bos in reversed(events):
        if latest-int(bos["index"]) > MAX_SETUP_AGE_15M+2: continue
        retest = _pullback_retest(candles,side,bos,MAX_SETUP_AGE_15M)
        if retest["valid"] and latest-int(retest["index"]) <= MAX_SETUP_AGE_15M:
            return bos,retest
    return None,{"valid":False,"index":None,"time":None,"level":None,"quality":0.0,"rejection":False,"low":None,"high":None}



def _fifteen_minute_entry_confirmation(
    candles: List[Candle],
    side: str,
    setup_level: Optional[float],
    retest_time: Optional[int],
    *,
    rsi_value: Optional[float] = None,
    rvol_value: Optional[float] = None,
    atr_value: Optional[float] = None,
) -> Dict[str, Any]:
    """Measure closed-15M candle quality after a valid BOS/retest.

    This is supporting analysis only; it is not a separate lower-timeframe
    confirmation gate.
    """
    empty = {
        "ready": False,
        "quality": 0.0,
        "rsi": 50.0,
        "rvol": 0.0,
        "atr": 0.0,
        "body_ratio": 0.0,
        "candle_time": 0,
        "trigger_type": "NONE",
        "reason": "insufficient data",
    }
    if len(candles) < 30 or side not in {"LONG", "SHORT"} or setup_level is None:
        return empty

    cur = candles[-1]
    prev = candles[-2]
    o = float(cur["open"])
    h = float(cur["high"])
    l = float(cur["low"])
    close = float(cur["close"])
    ph = float(prev["high"])
    pl = float(prev["low"])
    rng = max(h - l, 1e-12)
    body = abs(close - o) / rng
    closes = [float(c["close"]) for c in candles]
    r = float(rsi_value) if rsi_value is not None else _safe_rsi(closes)
    rv = float(rvol_value) if rvol_value is not None else _relative_volume(candles)
    a = float(atr_value) if atr_value is not None else _safe_atr(candles)

    if retest_time is not None and int(cur["time"]) <= int(retest_time):
        empty.update({"candle_time": int(cur["time"]), "rsi": r, "rvol": rv, "atr": a, "body_ratio": body,
                      "reason": "15M entry candle is not after retest"})
        return empty

    buffer = max(a * 0.05, 1e-12)
    close_location = (close - l) / rng if side == "LONG" else (h - close) / rng
    if side == "LONG":
        breakout = close > setup_level + buffer and close > o and close > ph
        reclaim = close > setup_level + buffer and close > o and l <= setup_level + buffer
        momentum = r >= 55.0
        trigger_type = "BREAKOUT" if breakout else "RECLAIM" if reclaim else "NONE"
        ready = (breakout or reclaim) and momentum and rv >= MIN_TRIGGER_RVOL and body >= MIN_TRIGGER_BODY and close_location >= 0.70
        momentum_quality = _clamp((r - 50.0) / 20.0, 0.0, 1.0)
    else:
        breakdown = close < setup_level - buffer and close < o and close < pl
        reclaim = close < setup_level - buffer and close < o and h >= setup_level - buffer
        momentum = r <= 45.0
        trigger_type = "BREAKDOWN" if breakdown else "RECLAIM" if reclaim else "NONE"
        ready = (breakdown or reclaim) and momentum and rv >= MIN_TRIGGER_RVOL and body >= MIN_TRIGGER_BODY and close_location >= 0.70
        momentum_quality = _clamp((50.0 - r) / 20.0, 0.0, 1.0)

    quality = _clamp(
        0.40 * _clamp(body / 0.70, 0.0, 1.0)
        + 0.30 * _clamp(rv / 1.50, 0.0, 1.0)
        + 0.30 * momentum_quality,
        0.0,
        1.0,
    )

    if ready:
        reason = "15M candle quality supportive"
    elif rv < MIN_TRIGGER_RVOL:
        reason = "15M RVOL is below the preferred level"
    elif body < MIN_TRIGGER_BODY:
        reason = "15M candle body is below the preferred level"
    elif close_location < 0.70:
        reason = "15M candle closed too far from directional extreme"
    elif side == "LONG" and r < 55.0:
        reason = "15M LONG momentum is below the preferred level"
    elif side == "SHORT" and r > 45.0:
        reason = "15M SHORT momentum is below the preferred level"
    else:
        reason = f"15M {side} breakout/reclaim condition not met"

    return {
        "ready": bool(ready),
        "quality": quality,
        "rsi": r,
        "rvol": rv,
        "atr": a,
        "body_ratio": body,
        "close_location": close_location,
        "candle_time": int(cur["time"]),
        "trigger_type": trigger_type,
        "reason": reason,
    }


def _level_clusters(candles: List[Candle], atr_value: float, lookback: int = 120):
    recent = candles[-lookback:] if len(candles) > lookback else candles
    if not recent:
        return None, None
    current = float(recent[-1]["close"])
    tolerance = max(atr_value * 0.20, 1e-12)
    highs = [float(c["high"]) for c in recent if float(c["high"]) > current + tolerance]
    lows = [float(c["low"]) for c in recent if float(c["low"]) < current - tolerance]
    return (max(lows) if lows else None, min(highs) if highs else None)


def _collect_structural_levels(frames, atr_value: float, entry: float, max_swings_per_frame: int = 15):
    raw = []
    for timeframe, candles in frames:
        if not candles:
            continue
        highs, lows = _swing_points(candles)
        for idx, p in highs[-max_swings_per_frame:]:
            if p > entry:
                raw.append({"price": float(p), "timeframe": timeframe, "index": idx, "kind": "RESISTANCE"})
        for idx, p in lows[-max_swings_per_frame:]:
            if p < entry:
                raw.append({"price": float(p), "timeframe": timeframe, "index": idx, "kind": "SUPPORT"})
    if not raw:
        return []
    tol = max(atr_value * 0.15, 1e-12)
    raw.sort(key=lambda x: x["price"])
    priority = {"1D": 4, "4H": 3, "1H": 2, "15M": 1}
    clusters = []
    for level in raw:
        if not clusters or abs(level["price"] - clusters[-1]["price"]) > tol:
            clusters.append(level.copy())
        elif priority.get(level["timeframe"], 0) > priority.get(clusters[-1]["timeframe"], 0):
            clusters[-1] = level.copy()
    return clusters



def _target_path(frames, side: str, entry: float, stop: float, atr_value: float):
    """Select ONE realistic higher-timeframe structural target.

    The final target must be a confirmed 1H/4H/1D swing level and must provide
    both the required ATR distance and minimum post-cost RR geometry.
    """
    risk = abs(entry - stop)
    base = {
        "ok": False,
        "tp": None,
        "obstacle": None,
        "risk": risk,
        "structural": False,
        "target_levels": [],
        "target_timeframe": None,
    }
    if risk <= 0 or atr_value <= 0:
        base["reason"] = "zero risk or ATR"
        return base

    levels = _collect_structural_levels(frames, atr_value, entry)
    clearance = max(0.30 * atr_value, 1e-12)
    major = {"1H", "4H", "1D"}
    ordered = [
        x for x in levels
        if (
            x["price"] > entry + clearance
            if side == "LONG"
            else x["price"] < entry - clearance
        )
    ]
    ordered.sort(key=lambda x: x["price"], reverse=(side == "SHORT"))
    base["target_levels"] = ordered[:16]

    if not ordered:
        base["reason"] = "no structural target"
        return base

    minimum_distance = max(MIN_TP_ATR * atr_value, MIN_RR * risk)
    candidates = [
        x for x in ordered
        if x.get("timeframe") in major
        and abs(float(x["price"]) - entry) >= minimum_distance
    ]
    if not candidates:
        base["reason"] = (
            f"no 1H/4H/1D target reaches {MIN_TP_ATR:.2f} ATR and "
            f"{MIN_RR:.2f}R"
        )
        return base

    target = candidates[0]
    target_price = float(target["price"])

    # The selected target is the first major structural level that clears the
    # geometry floor. A closer major level would have been selected instead.
    closer_major = [
        x for x in ordered
        if x.get("timeframe") in major
        and (
            x["price"] < target_price - clearance
            if side == "LONG"
            else x["price"] > target_price + clearance
        )
    ]
    if closer_major:
        base["reason"] = "major structural target ordering is ambiguous"
        return base

    base.update(
        {
            "ok": True,
            "tp": target_price,
            "obstacle": None,
            "reason": "single higher-timeframe structural target",
            "structural": True,
            "tp_level": target,
            "target_timeframe": str(target.get("timeframe") or ""),
        }
    )
    return base


def _rolling_vwap(candles: List[Candle], window: int = 48) -> float | None:
    rows = candles[-window:] if candles and len(candles) > window else (candles or [])
    total_volume = 0.0
    weighted = 0.0
    for candle in rows:
        try:
            high = float(candle["high"])
            low = float(candle["low"])
            close = float(candle["close"])
            volume = float(candle.get("volume", 0.0))
        except (TypeError, ValueError, KeyError):
            continue
        if volume <= 0 or not all(math.isfinite(v) for v in (high, low, close, volume)):
            continue
        typical = (high + low + close) / 3.0
        weighted += typical * volume
        total_volume += volume
    return (weighted / total_volume) if total_volume > 0 else None


def _backtest_flow_proxy(candles: List[Candle], window: int = 12) -> float | None:
    rows = candles[-window:] if candles and len(candles) > window else (candles or [])
    buy = sell = 0.0
    for candle in rows:
        try:
            open_price = float(candle["open"])
            close_price = float(candle["close"])
            volume = float(candle.get("volume", 0.0))
        except (TypeError, ValueError, KeyError):
            continue
        if volume <= 0 or not all(math.isfinite(v) for v in (open_price, close_price, volume)):
            continue
        if close_price > open_price:
            buy += volume
        elif close_price < open_price:
            sell += volume
    total = buy + sell
    return ((buy - sell) / total) if total > 0 else None




def _shock_veto(candles: List[Candle], side: str, atr_value: float) -> tuple[bool, str]:
    """Hard veto for abnormal 15M shock candles/liquidation-like moves."""
    if not candles or atr_value <= 0:
        return False, "shock veto cannot be evaluated"
    cur = candles[-1]
    try:
        o = float(cur["open"])
        h = float(cur["high"])
        l = float(cur["low"])
        c = float(cur["close"])
    except (KeyError, TypeError, ValueError):
        return False, "invalid 15M candle for shock veto"
    if not all(math.isfinite(v) for v in (o, h, l, c)) or o <= 0:
        return False, "invalid 15M candle for shock veto"
    range_atr = max(0.0, h - l) / atr_value
    body_atr = abs(c - o) / atr_value
    adverse_body = (o - c) / atr_value if side == "LONG" else (c - o) / atr_value
    if range_atr > 4.0:
        return False, f"15M shock range {range_atr:.2f} ATR > 4.00"
    if adverse_body > 2.0:
        return False, f"15M adverse shock body {adverse_body:.2f} ATR > 2.00"
    return True, "OK"

def evaluate_confirmation_families(data: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate eight optional supporting evidence families.

    Status values are PASS/FAIL/ABSTAIN. Missing live-only data abstains.
    Family counts and diversity are diagnostic/reporting evidence only; they
    never veto a structurally valid signal.
    """
    side = str(data.get("setup") or "").upper()
    result: Dict[str, Dict[str, Any]] = {}

    momentum = _num(data.get("momentum_quality"))
    result["momentum"] = {
        "status": "PASS" if side in {"LONG", "SHORT"} and momentum >= 0.55 else "FAIL",
        "value": momentum,
        "source": "RSI+MACD+15M momentum",
    }

    rvol = _num(data.get("rvol_15m", data.get("rvol")))
    result["relative_volume"] = {
        "status": "PASS" if rvol >= 1.0 else "FAIL",
        "value": rvol,
        "source": "15M RVOL",
    }

    vol_ok = bool(data.get("volatility_ok"))
    atr_rank = _num(data.get("atr_percentile"), 50.0)
    result["volatility_regime"] = {
        "status": "PASS" if vol_ok else "FAIL",
        "value": atr_rank,
        "source": "ATR percentile/regime",
    }

    spread = data.get("mexc_spread_pct")
    bid_depth = data.get("bid_depth")
    ask_depth = data.get("ask_depth")
    if spread is None and bid_depth is None and ask_depth is None:
        result["liquidity_quality"] = {
            "status": "ABSTAIN", "value": None, "source": "MEXC liquidity snapshot unavailable"
        }
    else:
        spread_v = _num(spread, 999.0)
        depth_total = _num(bid_depth) + _num(ask_depth)
        imbalance = abs(_num(data.get("orderbook_imbalance")))
        max_spread = _num(data.get("max_mexc_spread_pct"), 0.001)
        spread_pass = spread is not None and spread_v <= max_spread
        depth_ok = (bid_depth is None and ask_depth is None) or (depth_total > 0 and imbalance <= 0.85)
        result["liquidity_quality"] = {
            "status": "PASS" if spread_pass and depth_ok else "FAIL",
            "value": {"spread_pct": spread_v, "depth": depth_total, "imbalance": imbalance},
            "source": "MEXC ticker spread (+optional depth)",
        }

    funding = data.get("mexc_funding_rate")
    if funding is None:
        result["funding_crowding"] = {
            "status": "ABSTAIN", "value": None, "source": "funding unavailable"
        }
    else:
        funding_v = _num(funding)
        # Funding is used as a crowding veto/family, not a direction predictor.
        # 0.05% per interval is deliberately treated as crowded.
        result["funding_crowding"] = {
            "status": "PASS" if abs(funding_v) <= 0.0005 else "FAIL",
            "value": funding_v,
            "source": "MEXC funding rate",
        }

    flow = data.get("volume_delta_ratio")
    flow_source = "MEXC deals"
    if flow is None:
        candles = data.get("_candles_15m") or data.get("mexc_15m_rows") or []
        flow = _backtest_flow_proxy(candles)
        if flow is None and data.get("flow_proxy_ratio") is not None:
            flow = _num(data.get("flow_proxy_ratio"))
        flow_source = "15M candle volume proxy"
    if flow is None:
        result["flow_pressure"] = {
            "status": "ABSTAIN", "value": None, "source": "flow unavailable"
        }
    else:
        flow_v = _num(flow)
        aligned = flow_v >= 0.05 if side == "LONG" else flow_v <= -0.05 if side == "SHORT" else False
        result["flow_pressure"] = {
            "status": "PASS" if aligned else "FAIL",
            "value": flow_v,
            "source": flow_source,
        }

    tp = data.get("tp")
    atr = _num(data.get("atr"))
    tp_distance_atr = abs(_num(tp) - _num(data.get("entry"))) / atr if _num(tp) > 0 and _num(data.get("entry")) > 0 and atr > 0 else 0.0
    target_pass = bool(
        data.get("target_path_ok")
        and data.get("target_path_structural")
        and _num(tp) > 0
        and tp_distance_atr >= MIN_TP_ATR
    )
    result["htf_target_path"] = {
        "status": "PASS" if target_pass else "FAIL",
        "value": tp_distance_atr,
        "source": "1H/4H/1D structural target",
    }

    candles = data.get("_candles_15m") or data.get("mexc_15m_rows") or []
    vwap = _rolling_vwap(candles)
    if vwap is None and data.get("rolling_vwap_12h") is not None:
        vwap = _num(data.get("rolling_vwap_12h"))
    entry = _num(data.get("entry"))
    if vwap is None or entry <= 0 or atr <= 0 or side not in {"LONG", "SHORT"}:
        result["vwap_location"] = {
            "status": "ABSTAIN", "value": None, "source": "rolling VWAP unavailable"
        }
    else:
        vwap_distance_atr = abs(entry - vwap) / atr
        favorable = entry >= vwap if side == "LONG" else entry <= vwap
        # Avoid buying/selling a setup that is already excessively extended.
        result["vwap_location"] = {
            "status": "PASS" if favorable and vwap_distance_atr <= 1.5 else "FAIL",
            "value": vwap_distance_atr,
            "source": "12h rolling VWAP",
        }

    statuses = [item["status"] for item in result.values()]
    passed = sum(status == "PASS" for status in statuses)
    failed = sum(status == "FAIL" for status in statuses)
    available = passed + failed

    momentum_group = result["momentum"]["status"] == "PASS"
    flow_group = result["flow_pressure"]["status"] == "PASS" or result["funding_crowding"]["status"] == "PASS"
    liquidity_vol_group = (
        result["liquidity_quality"]["status"] == "PASS"
        or result["volatility_regime"]["status"] == "PASS"
    )
    diversity_ok = momentum_group and flow_group and liquidity_vol_group
    # Supporting families are evidence, not a mandatory gate. Missing optional
    # microstructure data must never manufacture a rejection of a structural setup.
    supporting_quality_ok = bool(passed >= 2 or available == 0)

    return {
        "families": result,
        "passed": passed,
        "failed": failed,
        "available": available,
        "required_passes": 0,
        "minimum_available": 0,
        "diversity_ok": diversity_ok,
        "supporting_quality_ok": supporting_quality_ok,
        "passed_ok": True,  # legacy field; intentionally non-gating
    }


def calculate_trade_levels(data: Dict[str, Any]) -> Dict[str, Any]:
    side = str(data.get("setup") or "").upper()
    entry = _num(data.get("price"))
    atr15 = _num(data.get("atr"))
    empty = {
        "entry": entry if entry > 0 else None,
        "stop_loss": None,
        "tp": None,
        "rr": None,
        "target_path_ok": False,
        "target_path_structural": False,
        "trade_geometry_ok": False,
    }
    if side not in {"LONG", "SHORT"} or entry <= 0 or atr15 <= 0:
        return empty

    retest = data.get("retest") or {}
    protected_low = _num(data.get("protected_low"), 0.0)
    protected_high = _num(data.get("protected_high"), 0.0)

    if side == "LONG":
        anchors: list[tuple[str, float]] = []
        if retest.get("low") is not None and _num(retest.get("low")) < entry:
            anchors.append(("15M_RETEST_LOW", _num(retest.get("low"))))
        if protected_low > 0 and protected_low < entry:
            anchors.append(("1H_PROTECTED_LOW", protected_low))
        if data.get("support") is not None and _num(data.get("support")) < entry:
            anchors.append(("STRUCTURAL_SUPPORT", _num(data.get("support"))))
        if not anchors:
            return empty | {"entry": entry, "geometry_reason": "no structural invalidation anchor"}
        # Use the deepest relevant invalidation point, then add a small ATR
        # safety buffer. A separate 1 ATR floor prevents scalp-tight stops.
        anchor = min(value for _, value in anchors)
        names = "+".join(name for name, _ in anchors)
        structural_stop = anchor - 0.15 * atr15
        swing_floor_stop = entry - MIN_SL_ATR * atr15
        stop = min(structural_stop, swing_floor_stop)
        stop_source = f"DEEPEST({names})+ATR_BUFFER"
    else:
        anchors = []
        if retest.get("high") is not None and _num(retest.get("high")) > entry:
            anchors.append(("15M_RETEST_HIGH", _num(retest.get("high"))))
        if protected_high > entry:
            anchors.append(("1H_PROTECTED_HIGH", protected_high))
        if data.get("resistance") is not None and _num(data.get("resistance")) > entry:
            anchors.append(("STRUCTURAL_RESISTANCE", _num(data.get("resistance"))))
        if not anchors:
            return empty | {"entry": entry, "geometry_reason": "no structural invalidation anchor"}
        anchor = max(value for _, value in anchors)
        names = "+".join(name for name, _ in anchors)
        structural_stop = anchor + 0.15 * atr15
        swing_floor_stop = entry + MIN_SL_ATR * atr15
        stop = max(structural_stop, swing_floor_stop)
        stop_source = f"DEEPEST({names})+ATR_BUFFER"

    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        return empty | {"entry": entry, "stop_source": stop_source, "geometry_reason": "stop is on wrong side of entry"}

    stop_distance = abs(entry - stop)
    stop_pct = stop_distance / entry
    stop_atr = stop_distance / atr15
    if stop_atr < MIN_SL_ATR or stop_atr > MAX_SL_ATR:
        return {
            **empty,
            "entry": entry,
            "stop_loss": float(stop),
            "sl_atr": stop_atr,
            "stop_distance_pct": stop_pct,
            "stop_source": stop_source,
            "geometry_reason": (
                f"stop distance {stop_atr:.2f} ATR outside "
                f"{MIN_SL_ATR:.2f}-{MAX_SL_ATR:.2f} ATR"
            ),
            "trade_geometry_ok": False,
        }

    frames = data.get("target_frames") or [("15M", data.get("_candles_15m", []))]
    path = _target_path(frames, side, entry, stop, atr15)
    tp = path.get("tp")
    risk = stop_distance
    gross_rr = abs(tp - entry) / risk if tp is not None and risk > 0 else None
    tp_distance = abs(tp - entry) if tp is not None else 0.0
    tp_atr = tp_distance / atr15 if atr15 > 0 else 0.0
    cost_pct = 0.0015
    cost_price = entry * cost_pct
    net_reward = max(0.0, tp_distance - cost_price)
    net_risk = risk + cost_price
    rr = (net_reward / net_risk) if net_risk > 0 else None

    geometry_ok = bool(
        path.get("ok")
        and path.get("structural")
        and tp is not None
        and rr is not None
        and tp_atr >= MIN_TP_ATR
        and rr >= MIN_RR
    )
    geometry_reason = "OK" if geometry_ok else str(path.get("reason") or "single-TP geometry failed")

    return {
        "entry": entry,
        "stop_loss": float(stop),
        "tp": float(tp) if tp is not None else None,
        "rr": rr,
        "rr_gross": gross_rr,
        "estimated_round_trip_cost_pct": cost_pct,
        "target_path_ok": bool(path.get("ok")),
        "target_path_structural": bool(path.get("structural")),
        "target_obstacle": path.get("obstacle"),
        "target_path_reason": path.get("reason"),
        "target_levels": path.get("target_levels", []),
        "target_timeframe": path.get("target_timeframe"),
        "stop_source": stop_source,
        "stop_distance_pct": stop_pct,
        "sl_atr": stop_atr,
        "tp_distance_pct": tp_distance / entry if entry > 0 else 0.0,
        "tp_distance_atr": tp_atr,
        "trade_geometry_ok": geometry_ok,
        "geometry_reason": geometry_reason,
    }


def _build_score(*, direction_ok, structure_ok, setup_ok, momentum_ok, volume_ok,
                 location_ok, futures_ok, volatility_ok, trigger_quality=0,
                 rvol=0, bos_quality=0, retest_quality=0, momentum_quality=None,
                 volume_quality=None, family_result=None):
    """Build a structure-first score; optional confirmation families are informational."""
    if momentum_quality is None:
        momentum_quality = 1.0 if momentum_ok else 0.0
    if volume_quality is None:
        volume_quality = 1.0 if volume_ok else _clamp(rvol / 1.50, 0.0, 1.0)

    setup_quality = _clamp(
        0.50 * _num(trigger_quality)
        + 0.25 * _num(bos_quality)
        + 0.25 * _num(retest_quality),
        0.0,
        1.0,
    )

    # 50 points: 4H regime + 1H directional structure + 15M BOS/retest.
    # This is the identity of the signal and cannot be diluted by optional data.
    structure_points = 50 if (direction_ok and structure_ok and setup_ok) else 0

    # 15 points: 15M setup quality (BOS/retest strength + candle quality).
    setup_points = int(round(15.0 * setup_quality)) if setup_ok else 0

    # 20 points: supporting analysis evidence, not exact price percentages and
    # not a hard lower-timeframe/family gate.
    analysis_quality = _clamp(
        0.35 * _num(momentum_quality)
        + 0.30 * _num(volume_quality)
        + 0.20 * (1.0 if volatility_ok else 0.0)
        + 0.15 * _num(trigger_quality),
        0.0,
        1.0,
    )
    analysis_points = int(round(20.0 * analysis_quality))

    # 15 points: structural target/risk geometry. RR remains mathematically
    # required by risk validation, but exact percent distances do not define a setup.
    target_points = 10 if location_ok else 0
    risk_points = 5 if (location_ok and direction_ok) else 0

    groups = {
        "structure_prerequisites": structure_points,
        "setup_quality": setup_points,
        "analysis_evidence": analysis_points,
        "structural_target": target_points,
        "risk_geometry": risk_points,
    }
    score = sum(groups.values())
    return max(0, min(100, int(score))), groups, int(
        family_result.get("passed", 0) if isinstance(family_result, dict) else 0
    )


def _data_quality(candles: List[Candle], timeframe_ms: int, minimum: int):
    if len(candles)<minimum: return False,f"not enough candles ({len(candles)}<{minimum})"
    times=[int(c["time"]) for c in candles[-minimum:]]
    if any(b<=a for a,b in zip(times,times[1:])): return False,"non-monotonic timestamps"
    if len(times)>=2 and times[-1]-times[-2]>timeframe_ms*2: return False,"recent candle gap"
    return True,"OK"


def build_btc_context(candles_4h,candles_1h,candles_15m):
    regime=_four_hour_regime(candles_4h); alignment=_one_hour_alignment(candles_1h,regime)
    close=[float(c["close"]) for c in candles_15m]; a=_safe_atr(candles_15m)
    move=(close[-1]-close[-2])/a if len(close)>=2 and a>0 else 0.0
    return {"ok":True,"bull_4h":bool(regime["bull"]),"bear_4h":bool(regime["bear"]),
            "regime_4h":regime["regime"],"structure_1h":alignment["structure"],
            "strong_bull_1h":bool(alignment["long"]),"strong_bear_1h":bool(alignment["short"]),
            "move_15m_atr":move,"candle_time_15m":int(candles_15m[-1]["time"])}


def btc_filter_ok(side: str, context: Dict[str,Any], *, is_btc: bool=False):
    if is_btc: return True,"BTC self-filter"
    if not context or not context.get("ok"): return False,"BTC context unavailable"
    side=side.upper(); move=_num(context.get("move_15m_atr"))
    if side=="LONG":
        if context.get("bear_4h"): return False,"BTC 4H bearish against LONG"
        if context.get("strong_bear_1h"): return False,"BTC 1H bearish against LONG"
        if move<=-BTC_SHOCK_ATR: return False,"BTC 15M shock against LONG"
    elif side=="SHORT":
        if context.get("bull_4h"): return False,"BTC 4H bullish against SHORT"
        if context.get("strong_bull_1h"): return False,"BTC 1H bullish against SHORT"
        if move>=BTC_SHOCK_ATR: return False,"BTC 15M shock against SHORT"
    else: return False,"Invalid side"
    return True,"OK"


def calculate_confluence(data: Dict[str,Any]) -> Dict[str,Any]:
    # Legacy compatibility only. The deterministic pipeline above is authoritative.
    result=dict(data); side=str(result.get("setup") or "").upper()
    trend=str(result.get("trend_4h") or "").upper()
    structure=str(result.get("structure_1h") or "").upper()
    bos_raw=result.get("bos_15m"); bos=str(bos_raw or "").upper()
    bullish=bool(bos_raw is True or bos_raw==1 or "BULLISH BOS" in bos)
    bearish=bool((bos_raw is False and bos_raw is not None) or bos_raw==-1 or "BEARISH BOS" in bos)
    ema_dir=str(result.get("ema_direction") or "").upper(); rv=_num(result.get("rsi"),50)
    score=0
    if side=="LONG":
        score=sum((trend=="BULLISH",structure=="HH/HL",bullish,ema_dir=="BULLISH",rv>50))
        if "BEARISH" in trend or structure=="LH/LL" or bearish or ema_dir=="BEARISH": result["setup"]="NO TRADE"
    elif side=="SHORT":
        score=sum((trend=="BEARISH",structure=="LH/LL",bearish,ema_dir=="BEARISH",rv<50))
        if "BULLISH" in trend or structure=="HH/HL" or bullish or ema_dir=="BULLISH": result["setup"]="NO TRADE"
    if str(result.get("volume") or "").upper()=="INCREASING": score+=1
    result["score"]=int(score)
    return result


def _direction_aligned(setup: str, regime: Dict[str, Any], alignment: Dict[str, Any]) -> bool:
    """Apply the user's trend rule: never counter-trend; sideways 4H needs very clear 1H."""
    if setup == "LONG":
        if regime.get("bull"):
            return bool(alignment.get("long"))
        if regime.get("bear"):
            return False
        return bool(alignment.get("long") and int(alignment.get("long_votes", 0)) >= 3)
    if setup == "SHORT":
        if regime.get("bear"):
            return bool(alignment.get("short"))
        if regime.get("bull"):
            return False
        return bool(alignment.get("short") and int(alignment.get("short_votes", 0)) >= 3)
    return False


def _diagnostic_failures(regime, alignment, long_candidate, short_candidate, bos_long, bos_short,
                         ret_long, ret_short, trigger_side, trigger, setup, momentum_ok, volume_ok,
                         location_ok, risk_ok, volatility_ok, score, families, supporting_family_count):
    failures=[]
    if regime.get("regime") == "NO_TRADE": failures.append("4H regime data")
    if not (alignment["long"] or alignment["short"]): failures.append("1H alignment")
    directional_15m = long_candidate or short_candidate
    if not directional_15m:
        if not bos_long and not bos_short:
            failures.append("15M BOS")
        elif not ret_long["valid"] and not ret_short["valid"]:
            failures.append("15M post-BOS retest")
        elif alignment["long"] or alignment["short"]:
            failures.append("15M directional setup")
    if setup in {"LONG", "SHORT"}:
        if not location_ok: failures.append("target path/location")
        if not risk_ok: failures.append("risk/RR")
        if score < MIN_SCORE: failures.append("score")
    if not failures:
        # Deterministic fallback: explain the first stage that stopped the pipeline.
        if regime.get("regime") == "NO_TRADE": failures.append("4H regime data")
        elif not (alignment["long"] or alignment["short"]): failures.append("1H alignment")
        elif not directional_15m: failures.append("15M directional setup")
        else: failures.append("technical hard gate")
    return list(dict.fromkeys(failures))



def analyze_candles(
    symbol: str,
    candles_4h: List,
    candles_1h: List,
    candles_15m: List,
    candles_5m: Optional[List] = None,
    candles_1d: Optional[List] = None,
    now_ms: Optional[int] = None,
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    c4 = closed_candle_rows(candles_4h, "4h", now)
    c1 = closed_candle_rows(candles_1h, "1h", now)
    c15 = closed_candle_rows(candles_15m, "15m", now)
    # ``candles_5m`` remains accepted for backward-compatible callers, but it is
    # intentionally ignored: lower-timeframe confirmation is not part of strategy.
    c1d = closed_candle_rows(candles_1d or [], "1d", now)
    for candles, tf, n in ((c4, "4h", 205), (c1, "1h", 205), (c15, "15m", 80)):
        ok, reason = _data_quality(candles, TIMEFRAME_MS[tf], n)
        if not ok:
            raise ValueError(f"{symbol}: {reason}")

    close4 = [float(c["close"]) for c in c4]
    close1 = [float(c["close"]) for c in c1]
    close15 = [float(c["close"]) for c in c15]
    price = close15[-1]

    cache = cache if cache is not None else {}

    progress_callback = cache.get("_BACKTEST_PROGRESS_CALLBACK")

    def report_progress(stage: str, details: Optional[Dict[str, Any]] = None) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(stage, details or {})
        except Exception:
            pass

    def _cache_key(tag: str, candles: List[Candle], extra: Any = None):
        last = candles[-1] if candles else None
        return (tag, len(candles), int(last["time"]) if last else 0, float(last["close"]) if last else 0.0, extra)

    regime_key = _cache_key("REGIME4", c4)
    regime = cache.get(regime_key)
    if regime is None:
        regime = _four_hour_regime(c4)
        cache[regime_key] = regime

    report_progress(
        "ENGINE_STAGE_4H_DONE",
        {"candles": len(c4), "regime": regime.get("regime")},
    )

    align_key = _cache_key("ALIGN1", c1, str(regime.get("regime") or "NO_TRADE"))
    alignment = cache.get(align_key)
    if alignment is None:
        alignment = _one_hour_alignment(c1, regime)
        cache[align_key] = alignment
    protected = alignment["protected"]
    structure1 = alignment["structure"]
    e21_1, e50_1 = alignment["e21"], alignment["e50"]

    report_progress(
        "ENGINE_STAGE_1H_DONE",
        {
            "candles": len(c1),
            "structure": structure1,
            "long_votes": alignment.get("long_votes"),
            "short_votes": alignment.get("short_votes"),
        },
    )

    # Reuse one ATR/swing pass for all 15M consumers in this analysis. A
    # backtest may additionally provide a full-history context; in that mode
    # BOS events are filtered to the causal prefix rather than rescanned.
    bt15 = cache.get("_BACKTEST_15M")

    prefix_count_15 = len(c15)
    full_context_matches = bool(
        isinstance(bt15, dict)
        and int(bt15.get("count", -1)) >= prefix_count_15
        and prefix_count_15 > 0
    )

    if full_context_matches:
        atr_full = bt15.get("atr") or []
        atr15 = float(atr_full[prefix_count_15 - 1]) if len(atr_full) >= prefix_count_15 else _safe_atr(c15)
        swing_full = bt15.get("swings")
        if isinstance(swing_full, tuple) and len(swing_full) == 2:
            full_highs, full_lows = swing_full
            max_visible = prefix_count_15 - 3
            swings15 = (
                [item for item in full_highs if item[0] <= max_visible],
                [item for item in full_lows if item[0] <= max_visible],
            )
        else:
            swings15 = _swing_points(c15)
        bos_events_long, bos_events_short = _slice_backtest_bos_events(
            bt15,
            prefix_count_15,
            70,
        )
    else:
        report_progress("ENGINE_15M_CONTEXT_START", {"candles": len(c15)})
        atr15_values = _atr_series(c15, 14)
        atr15 = float(atr15_values[-1]) if atr15_values else 0.0
        swings15 = _swing_points(c15)
        report_progress("ENGINE_15M_CONTEXT_DONE", {"candles": len(c15)})

        bos_long_key = _cache_key("BOS15_LONG", c15)
        bos_events_long = cache.get(bos_long_key)
        if bos_events_long is None:
            bos_events_long = _bos_events(
                c15,
                "LONG",
                atr_values=atr15_values,
                swings=swings15,
                progress_callback=progress_callback,
            )
            cache[bos_long_key] = bos_events_long
        bos_short_key = _cache_key("BOS15_SHORT", c15)
        bos_events_short = cache.get(bos_short_key)
        if bos_events_short is None:
            bos_events_short = _bos_events(
                c15,
                "SHORT",
                atr_values=atr15_values,
                swings=swings15,
                progress_callback=progress_callback,
            )
            cache[bos_short_key] = bos_events_short

    r15 = _safe_rsi(close15)
    rv15 = _relative_volume(c15)
    vol15 = volume_status(c15)
    bos_long, ret_long = _select_latest_bos_with_retest(c15, "LONG", bos_events_long)
    bos_short, ret_short = _select_latest_bos_with_retest(c15, "SHORT", bos_events_short)
    long_candidate = bool(alignment["long"] and bos_long and ret_long["valid"])
    short_candidate = bool(alignment["short"] and bos_short and ret_short["valid"])

    report_progress(
        "ENGINE_STAGE_15M_DONE",
        {
            "candles": len(c15),
            "long_candidate": long_candidate,
            "short_candidate": short_candidate,
            "bos_long": len(bos_events_long),
            "bos_short": len(bos_events_short),
        },
    )

    # Direction is selected from 4H/1H first. When both directions are present,
    # choose the stronger current BOS; the 15M candle then confirms that side.
    if long_candidate and not short_candidate:
        trigger_side = "LONG"
    elif short_candidate and not long_candidate:
        trigger_side = "SHORT"
    elif long_candidate and short_candidate:
        trigger_side = (
            "LONG"
            if _num((bos_long or {}).get("strength")) >= _num((bos_short or {}).get("strength"))
            else "SHORT"
        )
    else:
        trigger_side = "NONE"

    active_bos = bos_long if trigger_side == "LONG" else bos_short if trigger_side == "SHORT" else None
    active_retest = ret_long if trigger_side == "LONG" else ret_short if trigger_side == "SHORT" else None
    trigger_level = float(active_bos["level"]) if active_bos else None
    retest_time = int(active_retest["time"]) if active_retest and active_retest.get("time") is not None else None
    bos_quality = _num((active_bos or {}).get("strength"))
    retest_quality = _num((active_retest or {}).get("quality"))
    structure_quality_ok = bool(
        active_bos
        and active_retest
        and active_retest.get("valid")
        and bos_quality >= 0.65
        and retest_quality >= 0.70
    )

    entry_15m = _fifteen_minute_entry_confirmation(
        c15,
        trigger_side,
        trigger_level,
        retest_time,
        rsi_value=r15,
        rvol_value=rv15,
        atr_value=atr15,
    )

    report_progress(
        "ENGINE_STAGE_ENTRY_DONE",
        {"trigger_side": trigger_side, "ready": bool(entry_15m.get("ready"))},
    )

    # 15M BOS/retest is authoritative. There is no lower-timeframe confirmation gate.
    # 15M BOS/retest defines the setup; lower-timeframe confirmation is removed.
    setup = trigger_side if structure_quality_ok else "NO TRADE"

    sr_key = _cache_key("SRACTIONABLE", c15)
    if sr_key in cache:
        support, resistance = cache[sr_key]
    else:
        all_frames = [("1D", c1d), ("4H", c4), ("1H", c1), ("15M", c15)]
        structural_levels = _collect_structural_levels(all_frames, atr15, price)
        sr_clearance = max(0.35 * atr15, 1e-12)
        supports = [float(x["price"]) for x in structural_levels if float(x["price"]) <= price - sr_clearance]
        resistances = [float(x["price"]) for x in structural_levels if float(x["price"]) >= price + sr_clearance]
        support = max(supports) if supports else None
        resistance = min(resistances) if resistances else None
        cache[sr_key] = (support, resistance)

    atr_pct = _atr_percent(price, atr15)
    if full_context_matches and isinstance(bt15, dict):
        atr_rank = _atr_percentile_from_series(c15, bt15.get("atr") or [])
    else:
        atr_rank = _atr_percentile(c15)
    volatility_ok = bool(
        atr15 > 0
        and MIN_ATR_PERCENTILE <= atr_rank <= MAX_ATR_PERCENTILE
    )
    shock_veto_ok, shock_veto_reason = _shock_veto(c15, setup, atr15) if setup in {"LONG", "SHORT"} else (False, "no active setup")
    macd_line, macd_signal, macd_hist, macd_hist_delta = _macd_components(close15)
    # Momentum and volume are supporting evidence, not independent hard gates.
    if setup == "LONG":
        momentum_quality = _clamp((r15 - 48.0) / 20.0, 0.0, 1.0)
        if macd_hist > 0:
            momentum_quality = _clamp(momentum_quality + 0.20, 0.0, 1.0)
        if macd_hist_delta >= 0.0:
            momentum_quality = _clamp(momentum_quality + 0.10, 0.0, 1.0)
    elif setup == "SHORT":
        momentum_quality = _clamp((52.0 - r15) / 20.0, 0.0, 1.0)
        if macd_hist < 0:
            momentum_quality = _clamp(momentum_quality + 0.20, 0.0, 1.0)
        if macd_hist_delta <= 0.0:
            momentum_quality = _clamp(momentum_quality + 0.10, 0.0, 1.0)
    else:
        momentum_quality = 0.0
    momentum_ok = bool(momentum_quality >= 0.45)

    rvol_quality = _clamp((rv15 - 0.80) / 0.90, 0.0, 1.0)
    volume_quality = _clamp(
        0.80 * rvol_quality + 0.20 * (1.0 if vol15 == "INCREASING" else 0.0),
        0.0,
        1.0,
    )
    volume_ok = bool(volume_quality >= 0.55)
    # The supporting-family score is calculated later by the eight-family
    # evaluator; keep this field for compatibility with existing reports.
    supporting_family_count = 0
    ema21_15 = _safe_ema(close15, 21)
    extension_atr = (abs(price - ema21_15) / atr15) if ema21_15 is not None and atr15 > 0 else 999.0
    ema_extension_ok = bool(
        setup in {"LONG", "SHORT"}
        and extension_atr <= 1.25
        and ((setup == "LONG" and price >= ema21_15) or (setup == "SHORT" and price <= ema21_15))
    )

    levels = calculate_trade_levels({
        "setup": setup,
        "price": price,
        "atr": atr15,
        "protected_low": protected.get("protected_low"),
        "protected_high": protected.get("protected_high"),
        "support": support,
        "resistance": resistance,
        "retest": active_retest or {},
        "target_frames": [("1D", c1d), ("4H", c4), ("1H", c1), ("15M", c15)],
        "_candles_15m": c15,
    })

    report_progress(
        "ENGINE_STAGE_LEVELS_DONE",
        {
            "trade_geometry_ok": bool(levels.get("trade_geometry_ok")),
            "rr": levels.get("rr"),
            "stop_loss": levels.get("stop_loss"),
            "tp": levels.get("tp"),
        },
    )

    rr = levels.get("rr")
    risk_ok = bool(
        setup in {"LONG", "SHORT"}
        and levels.get("trade_geometry_ok")
        and levels.get("stop_loss") is not None
        and levels.get("tp") is not None
        and rr is not None
        and rr >= MIN_RR
    )
    entry_distance = (
        abs(price - float(active_retest["level"])) / atr15
        if active_retest and active_retest.get("level") is not None and atr15 > 0
        else 0.0
    )
    location_ok = bool(
        levels.get("target_path_ok")
        and levels.get("target_path_structural")
        and levels.get("tp") is not None
        and levels.get("tp_distance_atr", 0.0) >= MIN_TP_ATR
        and entry_distance <= MAX_ENTRY_DISTANCE_ATR
    )

    direction_ok = _direction_aligned(setup, regime, alignment)
    structure_ok = bool(
        ((setup == "LONG" and bos_long and ret_long["valid"])
        or (setup == "SHORT" and bos_short and ret_short["valid"]))
        and structure_quality_ok
    )
    # 4H/1H/15M are the structural prerequisites. The 15M candle-quality signal
    # is supporting evidence only; it cannot independently invalidate BOS/retest.
    setup_ok = bool(structure_ok)
    trigger_quality = _clamp(_num(entry_15m.get("quality")), 0.0, 1.0)

    family_result = evaluate_confirmation_families({
        "setup": setup,
        "momentum_quality": momentum_quality,
        "rvol_15m": rv15,
        "volatility_ok": volatility_ok,
        "target_path_ok": levels.get("target_path_ok"),
        "target_path_structural": levels.get("target_path_structural"),
        "entry": price,
        "tp": levels.get("tp"),
        "atr": atr15,
        "_candles_15m": c15,
        "mexc_spread_pct": None,
        "mexc_funding_rate": None,
        "orderbook_imbalance": None,
        "volume_delta_ratio": None,
        "bid_depth": None,
        "ask_depth": None,
    })

    score, groups, families = _build_score(
        direction_ok=direction_ok,
        structure_ok=structure_ok,
        setup_ok=setup_ok,
        momentum_ok=momentum_ok,
        volume_ok=volume_ok,
        location_ok=location_ok,
        futures_ok=False,
        volatility_ok=volatility_ok,
        trigger_quality=trigger_quality,
        rvol=rv15,
        bos_quality=_num((active_bos or {}).get("strength")),
        retest_quality=_num((active_retest or {}).get("quality")),
        momentum_quality=momentum_quality,
        volume_quality=volume_quality,
        family_result=family_result,
    )

    technical_candidate = bool(
        setup in {"LONG", "SHORT"}
        and direction_ok
        and structure_ok
        and setup_ok
        and location_ok
        and risk_ok
        and rr is not None
        and rr >= MIN_RR
        and shock_veto_ok
        and score >= MIN_SCORE
    )

    supporting_family_count = int(family_result.get("passed", 0))
    report_progress(
        "ENGINE_STAGE_SCORE_DONE",
        {
            "score": score,
            "families": families,
            "supporting_families": supporting_family_count,
            "available_families": family_result.get("available", 0),
            "family_diversity_ok": family_result.get("diversity_ok", False),
            "technical_candidate": technical_candidate,
        },
    )

    failures = _diagnostic_failures(
        regime,
        alignment,
        long_candidate,
        short_candidate,
        bos_long,
        bos_short,
        ret_long,
        ret_short,
        trigger_side,
        entry_15m,
        setup,
        momentum_ok,
        volume_ok,
        location_ok,
        risk_ok,
        volatility_ok,
        score,
        families,
        supporting_family_count,
    )
    # Geometry diagnostics are explicit because a high RR can otherwise hide
    # an unacceptably small stop.
    if setup in {"LONG", "SHORT"} and not levels.get("trade_geometry_ok"):
        failures.append("trade geometry")
    if setup in {"LONG", "SHORT"} and not shock_veto_ok:
        failures.append(f"shock/liquidity veto: {shock_veto_reason}")
    if setup in {"LONG", "SHORT"} and not structure_quality_ok:
        failures.append("BOS/retest quality")
    failures = list(dict.fromkeys(failures))

    reasons = []
    if regime["bull"]:
        reasons.append("4H bullish regime")
    if regime["bear"]:
        reasons.append("4H bearish regime")
    if alignment["long"]:
        reasons.append(f"1H bullish alignment ({alignment['long_votes']}/4)")
    if alignment["short"]:
        reasons.append(f"1H bearish alignment ({alignment['short_votes']}/4)")
    if active_bos:
        reasons.append(f"15M {trigger_side} BOS confirmed")
    if active_retest and active_retest.get("valid"):
        reasons.append(f"15M {trigger_side} retest confirmed")
    if entry_15m.get("ready"):
        reasons.append(f"15M {entry_15m.get('trigger_type', 'SETUP')} confirmation supportive")
    if momentum_ok:
        reasons.append("Momentum aligned")
    if volume_ok:
        reasons.append("15M volume participation supportive")
    if location_ok:
        reasons.append("Structural target path acceptable")
    if risk_ok and rr is not None:
        reasons.append(f"Risk acceptable ({rr:.2f}R)")
    if volatility_ok:
        reasons.append("Volatility acceptable")
    if shock_veto_ok:
        reasons.append("Shock veto clear")
    elif setup in {"LONG", "SHORT"}:
        reasons.append(shock_veto_reason)
    if not levels.get("trade_geometry_ok"):
        reasons.append(str(levels.get("geometry_reason") or "Trade geometry rejected"))
    if not technical_candidate:
        reasons.append("Technical hard gate failed")

    ema21_15, ema50_15 = _safe_ema(close15, 21), _safe_ema(close15, 50)
    daily_structure = _structure_from_swings(*_swing_points(c1d)) if len(c1d) >= 20 else "UNAVAILABLE"
    ema_direction = (
        "BULLISH" if (ema21_15 or 0) > (ema50_15 or 0)
        else "BEARISH" if (ema21_15 or 0) < (ema50_15 or 0)
        else "NEUTRAL"
    )

    report_progress(
        "ENGINE_STAGE_RETURN",
        {
            "technical_candidate": technical_candidate,
            "score": score,
            "families": families,
        },
    )

    return {
        "symbol": symbol,
        "price": price,
        "setup": setup,
        "setup_candidate": setup if setup in {"LONG", "SHORT"} else "NO TRADE",
        "trend_4h": regime.get("regime", "NO_TRADE"),
        "regime": regime["regime"],
        "daily_structure_1d": daily_structure,
        "structure_1h": structure1,
        "protected_structure_1h": protected["state"],
        "protected_high": protected.get("protected_high"),
        "protected_low": protected.get("protected_low"),
        "one_hour_long_votes": alignment["long_votes"],
        "one_hour_short_votes": alignment["short_votes"],
        "one_hour_evidence": {k: alignment.get(k, False) for k in ("long_ema", "short_ema", "long_structure", "short_structure", "long_momentum", "short_momentum", "long_slope", "short_slope")},
        "bos_15m": bool(active_bos),
        "bos_15m_time": active_bos.get("time") if active_bos else None,
        "bos_15m_index": active_bos.get("index") if active_bos else None,
        "bos_15m_strength": _num((active_bos or {}).get("strength")),
        "long_bos_level": bos_long.get("level") if bos_long else None,
        "short_bos_level": bos_short.get("level") if bos_short else None,
        "long_bos_event_count": len(bos_events_long),
        "short_bos_event_count": len(bos_events_short),
        "long_retest": bool(ret_long.get("valid")),
        "short_retest": bool(ret_short.get("valid")),
        "long_retest_time": ret_long.get("time"),
        "short_retest_time": ret_short.get("time"),
        "retest": active_retest or {},
        "ema21": ema21_15,
        "ema50": ema50_15,
        "ema21_4h": regime["e21"],
        "ema50_4h": regime["e50"],
        "ema100_4h": regime["e100"],
        "ema200_4h": regime["e200"],
        "ema21_1h": e21_1,
        "ema50_1h": e50_1,
        "ema200_1h": alignment["e200"],
        "ema_direction": ema_direction,
        "rsi": r15,
        "rsi_15m_entry": entry_15m.get("rsi", 50.0),
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "macd_hist_delta": macd_hist_delta,
        "atr": atr15,
        "atr_4h": regime["atr"],
        "atr_pct": atr_pct,
        "atr_percentile": atr_rank,
        "adx_4h": regime["adx"],
        "ema50_slope_4h": regime["slope"],
        "volume": vol15,
        "rvol": rv15,
        "rvol_15m": rv15,
        "support": support,
        "resistance": resistance,
        "futures_context": "PENDING",
        "futures_ok": False,
        "btc_filter_ok": False,
        "btc_filter_reason": "PENDING",
        "data_fresh": True,
        "signal_engine_version": ENGINE_VERSION,
        "signal_basis": "4H regime + 1H structure + 15M BOS/retest + ATR/structural geometry",
        "primary_entry_timeframe": "15M",
        "setup_timeframe": "15M",
        "intraday_max_hold_minutes": INTRADAY_MAX_HOLD_MINUTES,
        "trigger_side": trigger_side,
        "trigger_quality_15m": entry_15m.get("quality", 0.0),
        "entry_15m_close_location": entry_15m.get("close_location", 0.0),
        "structure_quality_ok": structure_quality_ok,
        "bos_quality_threshold": 0.65,
        "retest_quality_threshold": 0.70,
        "ema_extension_atr": extension_atr,
        "ema_extension_ok": ema_extension_ok,
        "trigger_quality": trigger_quality,
        "momentum_quality": momentum_quality,
        "volume_quality": volume_quality,
        "entry_15m_ready": bool(entry_15m.get("ready")),
        "entry_15m_type": entry_15m.get("trigger_type", "NONE"),
        "entry_15m_reason": entry_15m.get("reason", "not ready"),
        "closed_15m_candle_time": int(c15[-1]["time"]),
        "score": score,
        "score_groups": groups,
        "confirmation_family_count": families,
        "supporting_family_count": supporting_family_count,
        "min_supporting_family_count": MIN_SUPPORTING_FAMILIES,
        "bullish_points": int(regime["bull"]) + int(alignment["long"]) + int(e21_1 is not None and e50_1 is not None and e21_1 >= e50_1),
        "bearish_points": int(regime["bear"]) + int(alignment["short"]) + int(e21_1 is not None and e50_1 is not None and e21_1 <= e50_1),
        "direction_ok": direction_ok,
        "structure_ok": structure_ok,
        "setup_ok": setup_ok,
        "momentum_ok": momentum_ok,
        "volume_ok": volume_ok,
        "location_ok": location_ok,
        "volatility_ok": volatility_ok,
        "shock_veto_ok": shock_veto_ok,
        "shock_veto_reason": shock_veto_reason,
        "risk_ok": risk_ok,
        "sl_atr": levels.get("sl_atr", 0.0),
        "stop_distance_pct": levels.get("stop_distance_pct", 0.0),
        "stop_source": levels.get("stop_source"),
        # Legacy aliases are internal/storage compatibility only; strategy uses one TP.
        "tp": levels.get("tp"),
        "tp1": levels.get("tp"),
        "tp2": levels.get("tp"),
        "tp_distance_atr": levels.get("tp_distance_atr", 0.0),
        "tp_distance_pct": levels.get("tp_distance_pct", 0.0),
        "tp1_distance_atr": levels.get("tp_distance_atr", 0.0),
        "tp2_distance_atr": levels.get("tp_distance_atr", 0.0),
        "tp1_distance_pct": levels.get("tp_distance_pct", 0.0),
        "tp2_distance_pct": levels.get("tp_distance_pct", 0.0),
        "trade_geometry_ok": bool(levels.get("trade_geometry_ok")),
        "geometry_reason": levels.get("geometry_reason"),
        "target_path_ok": bool(levels.get("target_path_ok")),
        "target_path_structural": bool(levels.get("target_path_structural")),
        "target_path_reason": levels.get("target_path_reason"),
        "target_obstacle": levels.get("target_obstacle"),
        "target_levels": levels.get("target_levels", []),
        "technical_candidate": technical_candidate,
        "signal_blocked": not technical_candidate,
        "rejection_stage": None if technical_candidate else "TECHNICAL",
        "technical_gate_failures": failures,
        "reasons": reasons,
        "candle_time": int(c15[-1]["time"]),
        "setup_bos_time": active_bos.get("time") if active_bos else None,
        "setup_retest_time": active_retest.get("time") if active_retest else None,
        "rolling_vwap_12h": _rolling_vwap(c15),
        "flow_proxy_ratio": _backtest_flow_proxy(c15),
        **levels,
    }


async def analyze_symbol(market, symbol: str) -> Dict[str,Any]:
    ref=await market.resolve(symbol)
    c1d=await market.ohlcv(ref,"1D",100); c4h=await market.ohlcv(ref,"4H",250)
    c1h=await market.ohlcv(ref,"1H",250); c15=await market.ohlcv(ref,"15M",250)
    return analyze_candles(ref.symbol,c4h,c1h,c15,candles_1d=c1d)
