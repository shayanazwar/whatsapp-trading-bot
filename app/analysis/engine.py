from __future__ import annotations

"""Deterministic MEXC Futures multi-timeframe signal engine.

V1.7 - deterministic pipeline.

1D context -> 4H regime -> 1H directional evidence ->
15M BOS/retest -> 15M entry confirmation -> momentum/volume/volatility ->
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

MIN_SCORE = 75
MIN_RR = 2.0
MIN_FAMILIES = 4  # legacy/core-family display threshold; not the supporting gate
MIN_SUPPORTING_FAMILIES = 2
# Intraday geometry is structural. Percentage stop/target floors are deliberately
# absent. ATR is used only as a volatility/buffer sanity check around structure.
MIN_SL_ATR = 0.75
MAX_SL_ATR = 3.50
MIN_ATR_PERCENTILE = 20.0
MAX_ATR_PERCENTILE = 95.0
MAX_SETUP_AGE_15M = 24
MAX_ENTRY_DISTANCE_ATR = 3.00
BOS_BUFFER_ATR = 0.10
BOS_BUFFER_PCT = 0.0005
BTC_SHOCK_ATR = 1.50
ADX_TREND_MIN = 20.0
EMA_TOLERANCE_PCT = 0.0025
MIN_TRIGGER_RVOL = 1.10
MIN_TRIGGER_BODY = 0.55
RETEST_TOLERANCE_ATR = 0.35
RETEST_PENETRATION_ATR = 0.65
# TP1 is the first meaningful structural obstacle; TP2 must clear 2R.
MIN_TP1_ATR = 0.60
MIN_TP2_ATR = 2.00
INTRADAY_MAX_HOLD_MINUTES = 360
ENGINE_VERSION = "gold-v4.1-intraday-state-machine"
ENABLE_5M_REFINEMENT = False


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
                bull_votes >= 5 and bull_votes > bear_votes)
    bear = bool(current < e200 and e21 <= e50 and adx >= ADX_TREND_MIN and
                bear_votes >= 5 and bear_votes > bull_votes)
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
    tolerance = price * EMA_TOLERANCE_PCT
    long_ema = price >= e50 - tolerance and e21 >= e50
    short_ema = price <= e50 + tolerance and e21 <= e50
    long_structure = structure == "HH/HL" or protected["state"] == "BULLISH" or recent["bull_score"] >= 1
    short_structure = structure == "LH/LL" or protected["state"] == "BEARISH" or recent["bear_score"] >= 1
    long_momentum = r >= 50.0 and (e200 is None or price >= e200 * 0.995)
    short_momentum = r <= 50.0 and (e200 is None or price <= e200 * 1.005)
    long_slope = slope >= -0.0010
    short_slope = slope <= 0.0010
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
        buffer = max(a * BOS_BUFFER_ATR, close * BOS_BUFFER_PCT)
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
    tol, penetration = max(a*RETEST_TOLERANCE_ATR,abs(level)*0.001), max(a*RETEST_PENETRATION_ATR,abs(level)*0.0025)
    close_tol = max(a*0.20,abs(level)*0.0008)
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
    """Primary intraday entry confirmation on the closed 15M candle.

    The 15M timeframe owns entry confirmation. The 5M trigger is only a
    refinement and is never required to turn a valid intraday setup into a
    signal.
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

    buffer = max(a * 0.05, abs(setup_level) * 0.00025)
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
        reason = "confirmed 15M intraday entry"
    elif rv < MIN_TRIGGER_RVOL:
        reason = "15M RVOL below strict threshold"
    elif body < MIN_TRIGGER_BODY:
        reason = "15M candle body too weak"
    elif close_location < 0.70:
        reason = "15M candle closed too far from directional extreme"
    elif side == "LONG" and r < 55.0:
        reason = "15M LONG momentum below strict threshold"
    elif side == "SHORT" and r > 45.0:
        reason = "15M SHORT momentum below strict threshold"
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


def _five_minute_trigger(candles: List[Candle], side: str, setup_level: Optional[float]) -> Dict[str,Any]:
    empty={"ready":False,"long":False,"short":False,"quality":0.0,"rsi":50.0,"rvol":0.0,"atr":0.0,
           "candle_time":0,"body_ratio":0.0,"trigger_type":"NONE","reason":"insufficient data",
           "bos_level":None,"volume_expanding":False}
    if len(candles)<30: return empty
    cur=candles[-1]
    o,h,l,close=float(cur["open"]),float(cur["high"]),float(cur["low"]),float(cur["close"])
    rng=max(h-l,1e-12)
    body=abs(close-o)/rng
    closes=[float(c["close"]) for c in candles]
    r,rv,a=_safe_rsi(closes),_relative_volume(candles),_safe_atr(candles)
    prior=candles[-6:-1]
    prior_high=max(float(c["high"]) for c in prior) if prior else 0.0
    prior_low=min(float(c["low"]) for c in prior) if prior else 0.0
    prior_avg_volume=(sum(float(c["volume"]) for c in candles[-6:-1])/5.0) if len(candles)>=6 else 0.0
    volume_expanding=bool(prior_avg_volume>0 and float(cur["volume"])>prior_avg_volume)
    buffer=max(a*0.05, abs(close)*0.00025)
    close_location_long=(close-l)/rng
    close_location_short=(h-close)/rng
    long_level_ok=setup_level is None or close>setup_level+buffer
    short_level_ok=setup_level is None or close<setup_level-buffer
    # Continuation BOS: current closed 5M candle must clear the recent 5-bar
    # extreme and the 15M BOS level. This is causal and needs no future pivot.
    bos_long=bool(close>prior_high+buffer and long_level_ok and close>o)
    bos_short=bool(close<prior_low-buffer and short_level_ok and close<o)
    strong_long=bool(body>=MIN_TRIGGER_BODY and close_location_long>=0.70 and r>=55.0)
    strong_short=bool(body>=MIN_TRIGGER_BODY and close_location_short>=0.70 and r<=45.0)
    long_ok=bos_long and strong_long and rv>=MIN_TRIGGER_RVOL and volume_expanding
    short_ok=bos_short and strong_short and rv>=MIN_TRIGGER_RVOL and volume_expanding
    if side=="LONG":
        ready=long_ok
        q=_clamp(0.35*_clamp(body/0.70,0,1)+0.25*_clamp(rv/1.50,0,1)+0.20*_clamp(r/70.0,0,1)+0.20*(1.0 if volume_expanding else 0.0),0,1)
        trigger_type="BOS_CONTINUATION" if bos_long else "NONE"
        bos_level=float(max(prior_high, setup_level or prior_high))
    elif side=="SHORT":
        ready=short_ok
        q=_clamp(0.35*_clamp(body/0.70,0,1)+0.25*_clamp(rv/1.50,0,1)+0.20*_clamp((100.0-r)/70.0,0,1)+0.20*(1.0 if volume_expanding else 0.0),0,1)
        trigger_type="BOS_CONTINUATION" if bos_short else "NONE"
        bos_level=float(min(prior_low, setup_level if setup_level is not None else prior_low))
    else:
        ready,q,trigger_type,bos_level=False,0.0,"NONE",None
    if ready: reason="confirmed 5M BOS + strong candle + expanding volume"
    elif side=="NONE": reason="not evaluated: no directional 15M setup"
    elif not (bos_long if side=="LONG" else bos_short): reason="5M continuation BOS not confirmed"
    elif body<MIN_TRIGGER_BODY: reason="5M candle body too weak"
    elif (close_location_long<0.70 if side=="LONG" else close_location_short<0.70): reason="5M close location too weak"
    elif rv<MIN_TRIGGER_RVOL: reason="5M relative volume below threshold"
    elif not volume_expanding: reason="5M volume is not expanding"
    elif side=="LONG" and r<55.0: reason="5M LONG momentum below threshold"
    elif side=="SHORT" and r>45.0: reason="5M SHORT momentum below threshold"
    else: reason=f"5M {side} continuation condition not met"
    return {"ready":bool(ready),"long":bool(long_ok),"short":bool(short_ok),"quality":q,
            "rsi":r,"rvol":rv,"atr":a,"candle_time":int(cur["time"]),"body_ratio":body,
            "close_location":close_location_long if side=="LONG" else close_location_short,
            "trigger_type":trigger_type,"reason":reason,"bos_level":bos_level,
            "volume_expanding":volume_expanding}



def _level_clusters(candles: List[Candle], atr_value: float, lookback: int = 120):
    recent = candles[-lookback:] if len(candles) > lookback else candles
    if not recent:
        return None, None
    current = float(recent[-1]["close"])
    tolerance = max(atr_value * 0.20, current * 0.001)
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
    tol = max(atr_value * 0.15, entry * 0.0005)
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
    """Select TP1/TP2 from real structural levels; no fixed price-percent floors."""
    risk = abs(entry - stop)
    base = {
        "ok": False, "tp1": None, "tp2": None, "obstacle": None,
        "risk": risk, "structural": False, "target_levels": [],
    }
    if risk <= 0 or atr_value <= 0:
        base["reason"] = "zero risk or ATR"
        return base

    levels = _collect_structural_levels(frames, atr_value, entry)
    clearance = max(0.30 * atr_value, entry * 0.0005)
    ordered = [
        x for x in levels
        if (x["price"] > entry + clearance if side == "LONG" else x["price"] < entry - clearance)
    ]
    ordered.sort(key=lambda x: x["price"], reverse=(side == "SHORT"))
    base["target_levels"] = ordered[:12]
    if not ordered:
        base["reason"] = "no confirmed structural target"
        return base

    # TP1 is the nearest meaningful obstacle. It is not fabricated and need not
    # satisfy an arbitrary percentage target. The only floor is a small ATR-based
    # separation so the target is not effectively the entry itself.
    tp1_level = None
    for level in ordered:
        distance = abs(float(level["price"]) - entry)
        if distance >= max(MIN_TP1_ATR * atr_value, clearance):
            tp1_level = level
            break
    if tp1_level is None:
        base["reason"] = "nearest structural obstacle is too close"
        return base
    tp1 = float(tp1_level["price"])
    base["obstacle"] = tp1

    # TP2 prefers a meaningful higher-timeframe structural level. When no
    # 1H/4H/1D level can satisfy the swing-distance + RR requirements, fall back
    # to the next valid 15M structural level rather than rejecting a tradable
    # path solely because a higher-timeframe level is absent.
    major = {"1H", "4H", "1D"}
    tp2_level = None
    minimum_distance = max(MIN_TP2_ATR * atr_value, MIN_RR * risk)
    for require_major in (True, False):
        for level in ordered:
            price = float(level["price"])
            farther = price > tp1 + clearance if side == "LONG" else price < tp1 - clearance
            if not farther:
                continue
            if require_major and str(level.get("timeframe")) not in major:
                continue
            if abs(price - entry) >= minimum_distance:
                tp2_level = level
                break
        if tp2_level is not None:
            break

    if tp2_level is None:
        base.update({
            "tp1": tp1,
            "reason": "no structural TP2 reaches minimum swing distance and RR",
            "structural": True,
            "tp1_level": tp1_level,
        })
        return base

    tp2 = float(tp2_level["price"])
    base.update({
        "ok": True,
        "tp1": tp1,
        "tp2": tp2,
        "reason": "structural TP1 + higher-timeframe structural TP2",
        "structural": True,
        "tp1_level": tp1_level,
        "tp2_level": tp2_level,
    })
    return base

def calculate_trade_levels(data: Dict[str, Any]) -> Dict[str, Any]:
    side = str(data.get("setup") or "").upper()
    entry = _num(data.get("price"))
    atr15 = _num(data.get("atr"))
    empty = {
        "entry": entry if entry > 0 else None,
        "stop_loss": None,
        "tp1": None,
        "tp2": None,
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
        anchor = min(value for _, value in anchors)
        names = "+".join(name for name, _ in anchors)
        stop_source = f"DEEPEST({names})"
        structural_stop = anchor - 0.12 * atr15
        swing_floor_stop = entry - MIN_SL_ATR * atr15
        stop = min(structural_stop, swing_floor_stop)
    else:
        anchors: list[tuple[str, float]] = []
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
        stop_source = f"DEEPEST({names})"
        structural_stop = anchor + 0.12 * atr15
        swing_floor_stop = entry + MIN_SL_ATR * atr15
        stop = max(structural_stop, swing_floor_stop)

    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        return empty | {"entry": entry, "stop_source": stop_source, "geometry_reason": "stop is on wrong side of entry"}

    stop_distance = abs(entry - stop)
    stop_pct = stop_distance / entry
    stop_atr = stop_distance / atr15
    geometry_reason = "OK"
    if stop_atr < MIN_SL_ATR:
        geometry_reason = f"stop distance {stop_atr:.2f} ATR below structural safety floor"
    elif stop_atr > MAX_SL_ATR:
        geometry_reason = f"stop distance {stop_atr:.2f} ATR above intraday volatility bound"

    if geometry_reason != "OK":
        return {
            **empty,
            "entry": entry,
            "stop_loss": float(stop),
            "sl_atr": stop_atr,
            "stop_distance_pct": stop_pct,
            "stop_source": stop_source,
            "geometry_reason": geometry_reason,
            "trade_geometry_ok": False,
        }

    frames = data.get("target_frames") or [("15M", data.get("_candles_15m", []))]
    path = _target_path(frames, side, entry, stop, atr15)
    tp1, tp2 = path.get("tp1"), path.get("tp2")
    risk = stop_distance
    rr = abs(tp2 - entry) / risk if tp2 is not None and risk > 0 else None
    tp1_distance = abs(tp1 - entry) if tp1 is not None else 0.0
    tp2_distance = abs(tp2 - entry) if tp2 is not None else 0.0
    tp1_atr = tp1_distance / atr15 if atr15 > 0 else 0.0
    tp2_atr = tp2_distance / atr15 if atr15 > 0 else 0.0

    geometry_ok = bool(
        path.get("ok")
        and path.get("structural")
        and tp1 is not None
        and tp2 is not None
        and rr is not None
        and tp1_atr >= MIN_TP1_ATR
        and tp2_atr >= MIN_TP2_ATR
        and rr >= MIN_RR
    )
    if not geometry_ok and geometry_reason == "OK":
        geometry_reason = str(path.get("reason") or "target path failed intraday geometry")

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
        "stop_source": stop_source,
        "stop_distance_pct": stop_pct,
        "sl_atr": stop_atr,
        "tp1_distance_pct": tp1_distance / entry if entry > 0 else 0.0,
        "tp2_distance_pct": tp2_distance / entry if entry > 0 else 0.0,
        "tp1_distance_atr": tp1_atr,
        "tp2_distance_atr": tp2_atr,
        "trade_geometry_ok": geometry_ok,
        "geometry_reason": geometry_reason,
    }


def _build_score(*,direction_ok,structure_ok,setup_ok,momentum_ok,volume_ok,location_ok,
                 futures_ok,volatility_ok,trigger_quality=0,rvol=0,bos_quality=0,
                 retest_quality=0,momentum_quality=None,volume_quality=None):
    """Grade supporting evidence instead of making every family a hard gate."""
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
    groups = {
        "direction_regime": 20 if direction_ok else 0,
        "market_structure": 20 if structure_ok else 0,
        "setup_entry_trigger": 20 if setup_ok else 0,
        "momentum": int(round(10 * _clamp(_num(momentum_quality), 0.0, 1.0))) if setup_ok else 0,
        "volume_participation": int(round(10 * _clamp(_num(volume_quality), 0.0, 1.0))) if setup_ok else 0,
        "location_target_path": 10 if location_ok else 0,
        "futures_market_context": 5 if futures_ok else 0,
        "volatility_execution": 5 if volatility_ok else 0,
    }
    if groups["setup_entry_trigger"] and setup_quality < 0.55:
        groups["setup_entry_trigger"] = max(15, groups["setup_entry_trigger"] - 5)

    families = sum(bool(x) for x in (
        direction_ok,
        structure_ok,
        setup_ok,
        location_ok,
        _num(momentum_quality) >= 0.55,
        _num(volume_quality) >= 0.55,
    ))
    return max(0, min(100, sum(groups.values()))), groups, families


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
        return bool(alignment.get("long") and int(alignment.get("long_votes", 0)) == 4)
    if setup == "SHORT":
        if regime.get("bear"):
            return bool(alignment.get("short"))
        if regime.get("bull"):
            return False
        return bool(alignment.get("short") and int(alignment.get("short_votes", 0)) == 4)
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
        if supporting_family_count < MIN_SUPPORTING_FAMILIES:
            failures.append("supporting confirmation families")
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
    c5 = closed_candle_rows(candles_5m or [], "5m", now)
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

    # 5M is intentionally disabled in the signal decision path. 15M BOS/retest
    # is the authoritative setup/entry timeframe; 5M cannot affect eligibility,
    # score, family count, rejection reasons, or signal identity.
    if ENABLE_5M_REFINEMENT and len(c5) >= 30 and not bool(cache.get("_BACKTEST_DISABLE_5M_CONFIRMATION")):
        refinement_5m = _five_minute_trigger(c5, trigger_side, trigger_level)
    else:
        refinement_5m = {
            "ready": False, "long": False, "short": False, "quality": 0.0, "rsi": 50.0,
            "rvol": 0.0, "atr": 0.0, "candle_time": 0, "body_ratio": 0.0,
            "trigger_type": "DISABLED", "reason": "5M refinement disabled; 15M is authoritative",
            "bos_level": None, "volume_expanding": False,
        }
    if refinement_5m.get("ready") and retest_time is not None and int(refinement_5m["candle_time"]) < retest_time:
        refinement_5m = dict(refinement_5m)
        refinement_5m.update({"ready": False, "long": False, "short": False, "trigger_type": "INVALID_BEFORE_RETEST"})

    report_progress(
        "ENGINE_STAGE_5M_DONE",
        {
            "ready": bool(refinement_5m.get("ready")),
            "trigger_type": refinement_5m.get("trigger_type", "NONE"),
        },
    )

    # 15M BOS/retest defines the setup; 5M is optional refinement only.
    setup = trigger_side if structure_quality_ok else "NO TRADE"

    sr_key = _cache_key("SRACTIONABLE", c15)
    if sr_key in cache:
        support, resistance = cache[sr_key]
    else:
        all_frames = [("1D", c1d), ("4H", c4), ("1H", c1), ("15M", c15)]
        structural_levels = _collect_structural_levels(all_frames, atr15, price)
        sr_clearance = max(0.35 * atr15, price * 0.0005)
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
        and 0.0005 <= atr_pct <= 0.05
    )
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
    supporting_family_count = sum((
        momentum_quality >= 0.45,
        volume_quality >= 0.50,
        bool(_num(entry_15m.get("quality")) >= 0.50),
        volatility_ok,
    ))
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
            "tp2": levels.get("tp2"),
        },
    )

    rr = levels.get("rr")
    risk_ok = bool(
        setup in {"LONG", "SHORT"}
        and levels.get("trade_geometry_ok")
        and levels.get("stop_loss") is not None
        and levels.get("tp2") is not None
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
        and levels.get("trade_geometry_ok")
        and entry_distance <= MAX_ENTRY_DISTANCE_ATR
    )

    direction_ok = _direction_aligned(setup, regime, alignment)
    structure_ok = bool(
        ((setup == "LONG" and bos_long and ret_long["valid"])
        or (setup == "SHORT" and bos_short and ret_short["valid"]))
        and structure_quality_ok
    )
    # Core 15M setup. 5M can improve diagnostics but cannot invalidate the setup.
    setup_ok = bool(structure_ok)
    # The 15M setup owns signal eligibility. 5M is diagnostic/refinement only
    # and must not influence the score or any hard eligibility decision.
    trigger_quality = _clamp(_num(entry_15m.get("quality")), 0.0, 1.0)

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
        and score >= MIN_SCORE
        and supporting_family_count >= MIN_SUPPORTING_FAMILIES
    )

    report_progress(
        "ENGINE_STAGE_SCORE_DONE",
        {
            "score": score,
            "families": families,
            "supporting_families": supporting_family_count,
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
    if refinement_5m.get("ready"):
        reasons.append("Optional 5M refinement confirmed")
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
        "rsi_5m": refinement_5m.get("rsi", 50.0),
        "rsi_15m_entry": entry_15m.get("rsi", 50.0),
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "macd_hist_delta": macd_hist_delta,
        "atr": atr15,
        "atr_4h": regime["atr"],
        "atr_5m": refinement_5m.get("atr", 0.0),
        "atr_pct": atr_pct,
        "atr_percentile": atr_rank,
        "adx_4h": regime["adx"],
        "ema50_slope_4h": regime["slope"],
        "volume": vol15,
        "rvol": rv15,
        "rvol_15m": rv15,
        "rvol_5m": refinement_5m.get("rvol", 0.0),
        "support": support,
        "resistance": resistance,
        "futures_context": "PENDING",
        "futures_ok": False,
        "btc_filter_ok": False,
        "btc_filter_reason": "PENDING",
        "data_fresh": True,
        "signal_engine_version": ENGINE_VERSION,
        "primary_entry_timeframe": "15M",
        "setup_timeframe": "15M",
        "intraday_max_hold_minutes": INTRADAY_MAX_HOLD_MINUTES,
        "trigger_side": trigger_side,
        "trigger_5m": "BOS_CONTINUATION" if refinement_5m.get("ready") else "OPTIONAL",
        "trigger_type_5m": refinement_5m.get("trigger_type", "NONE"),
        "five_minute_bos_level": refinement_5m.get("bos_level"),
        "five_minute_volume_expanding": bool(refinement_5m.get("volume_expanding")),
        "trigger_reason_5m": refinement_5m.get("reason", "not required"),
        "trigger_quality_5m": refinement_5m.get("quality", 0.0),
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
        "five_minute_ready": bool(refinement_5m.get("ready")),
        "five_minute_refinement_enabled": ENABLE_5M_REFINEMENT,
        "five_minute_close_location": refinement_5m.get("close_location", 0.0),
        "five_minute_long": bool(refinement_5m.get("long")),
        "five_minute_short": bool(refinement_5m.get("short")),
        "closed_5m_candle_time": refinement_5m.get("candle_time", 0),
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
        "risk_ok": risk_ok,
        "sl_atr": levels.get("sl_atr", 0.0),
        "stop_distance_pct": levels.get("stop_distance_pct", 0.0),
        "stop_source": levels.get("stop_source"),
        "tp1_distance_atr": levels.get("tp1_distance_atr", 0.0),
        "tp2_distance_atr": levels.get("tp2_distance_atr", 0.0),
        "tp1_distance_pct": levels.get("tp1_distance_pct", 0.0),
        "tp2_distance_pct": levels.get("tp2_distance_pct", 0.0),
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
        **levels,
    }


async def analyze_symbol(market, symbol: str) -> Dict[str,Any]:
    ref=await market.resolve(symbol)
    c1d=await market.ohlcv(ref,"1D",100); c4h=await market.ohlcv(ref,"4H",250)
    c1h=await market.ohlcv(ref,"1H",250); c15=await market.ohlcv(ref,"15M",250); c5=await market.ohlcv(ref,"5M",250)
    return analyze_candles(ref.symbol,c4h,c1h,c15,c5,c1d)
