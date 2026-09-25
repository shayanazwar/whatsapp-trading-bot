from __future__ import annotations

"""Deterministic MEXC Futures multi-timeframe signal engine.

Design goals:
- closed-candle only decision making
- 1D context -> 4H regime -> 1H direction -> 15M BOS/retest -> 5M trigger
- genuine post-BOS chronology (the previous engine's critical bug is fixed)
- hard risk/location/volatility gates before scoring
- grouped 100-point evidence score; score is NOT probability
- futures context is supplied by scanner.py and remains pending here
- no AI, prediction, repainting, or future-looking swing confirmation

This module deliberately does not place orders.
"""

from typing import Any, Dict, List, Optional, Tuple
import math
import time

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
}

MIN_SCORE = 82
MIN_RR = 2.0
MIN_FAMILIES = 5
MIN_SL_ATR = 0.50
MAX_SL_ATR = 1.80
MAX_ATR_PERCENTILE = 95.0
MIN_ATR_PERCENTILE = 20.0
MAX_SETUP_AGE_15M = 8
MAX_ENTRY_DISTANCE_ATR = 1.20
BOS_BUFFER_ATR = 0.10
BOS_BUFFER_PCT = 0.0005


def _num(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def convert_candles(rows: List) -> List[Dict]:
    out: list[Dict] = []
    for row in rows or []:
        if isinstance(row, dict):
            t = row.get("time", row.get("timestamp", row.get("openTime", row.get("ts"))))
            o = row.get("open")
            h = row.get("high")
            l = row.get("low")
            c = row.get("close")
            v = row.get("volume", row.get("vol", 0))
        else:
            if len(row) < 6:
                continue
            t, o, h, l, c, v = row[:6]
        try:
            ts = int(float(t))
            if ts < 10**12:
                ts *= 1000
            candle = {
                "time": ts,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v or 0),
            }
            if candle["high"] <= 0 or candle["low"] <= 0 or candle["close"] <= 0:
                continue
            if candle["low"] > candle["high"]:
                continue
            out.append(candle)
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["time"])
    return out


def closed_candle_rows(candles: List[Dict], timeframe_ms: int, now_ms: Optional[int] = None) -> List[Dict]:
    """Remove the currently forming candle when timestamps are aligned."""
    if not candles:
        return []
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    out = list(candles)
    last = out[-1]
    # MEXC candle timestamps are treated as open timestamps.
    if last["time"] + timeframe_ms > now:
        out = out[:-1]
    return out


def _safe_ema(values: List[float], period: int) -> Optional[float]:
    try:
        value = ema(values, period)
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return float(value[-1]) if value else None
        return float(value)
    except Exception:
        return None


def _ema_series(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [sum(values[:period]) / period]
    for value in values[period:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _ema_slope(values: List[float], period: int, lookback: int = 5) -> float:
    series = _ema_series(values, period)
    if len(series) <= lookback:
        return 0.0
    base = abs(series[-lookback - 1])
    return (series[-1] - series[-lookback - 1]) / base if base else 0.0


def _safe_rsi(values: List[float], period: int = 14) -> float:
    try:
        value = rsi(values, period)
        if isinstance(value, (list, tuple)):
            value = value[-1] if value else 50.0
        return _num(value, 50.0)
    except Exception:
        return 50.0


def _safe_atr(candles: List[Dict], period: int = 14) -> float:
    try:
        value = atr(candles, period)
        if isinstance(value, (list, tuple)):
            value = value[-1] if value else 0.0
        return max(0.0, _num(value))
    except Exception:
        return 0.0


def _atr_percent(price: float, atr_value: float) -> float:
    return atr_value / price if price > 0 else 0.0


def _true_ranges(candles: List[Dict]) -> List[float]:
    tr: list[float] = []
    prev = None
    for c in candles:
        if prev is None:
            tr.append(c["high"] - c["low"])
        else:
            tr.append(max(c["high"] - c["low"], abs(c["high"] - prev), abs(c["low"] - prev)))
        prev = c["close"]
    return tr


def _adx(candles: List[Dict], period: int = 14) -> float:
    if len(candles) < period * 2 + 1:
        return 0.0
    trs: list[float] = []
    plus: list[float] = []
    minus: list[float] = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        up = cur["high"] - prev["high"]
        down = prev["low"] - cur["low"]
        trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])))
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    if len(trs) < period:
        return 0.0
    dx: list[float] = []
    atr_s = sum(trs[:period]) / period
    p_s = sum(plus[:period]) / period
    m_s = sum(minus[:period]) / period
    for i in range(period, len(trs)):
        atr_s = (atr_s * (period - 1) + trs[i]) / period
        p_s = (p_s * (period - 1) + plus[i]) / period
        m_s = (m_s * (period - 1) + minus[i]) / period
        pdi = 100.0 * p_s / atr_s if atr_s else 0.0
        mdi = 100.0 * m_s / atr_s if atr_s else 0.0
        dx.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    if len(dx) < period:
        return 0.0
    return sum(dx[:period]) / period if len(dx) == period else _wilder_last(dx, period)


def _wilder_last(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) <= period:
        return sum(values) / len(values)
    smoothed = sum(values[:period]) / period
    for x in values[period:]:
        smoothed = (smoothed * (period - 1) + x) / period
    return smoothed


def _relative_volume(candles: List[Dict], lookback: int = 20) -> float:
    if len(candles) < lookback + 1:
        return 0.0
    current = candles[-1]["volume"]
    avg = sum(c["volume"] for c in candles[-lookback - 1:-1]) / lookback
    return current / avg if avg > 0 else 0.0


def _atr_percentile(candles: List[Dict], period: int = 14, lookback: int = 100) -> float:
    if len(candles) < period + lookback + 1:
        return 50.0
    values: list[float] = []
    for end in range(len(candles) - lookback, len(candles)):
        window = candles[:end + 1]
        a = _safe_atr(window, period)
        p = window[-1]["close"]
        if p > 0 and a > 0:
            values.append(a / p)
    if not values:
        return 50.0
    current = values[-1]
    return 100.0 * sum(v <= current for v in values) / len(values)


def _swing_highs(candles: List[Dict], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    result: list[Tuple[int, float]] = []
    for i in range(left, len(candles) - right):
        h = candles[i]["high"]
        if all(h > candles[j]["high"] for j in range(i - left, i)) and all(h > candles[j]["high"] for j in range(i + 1, i + right + 1)):
            result.append((i, h))
    return result


def _swing_lows(candles: List[Dict], left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    result: list[Tuple[int, float]] = []
    for i in range(left, len(candles) - right):
        low = candles[i]["low"]
        if all(low < candles[j]["low"] for j in range(i - left, i)) and all(low < candles[j]["low"] for j in range(i + 1, i + right + 1)):
            result.append((i, low))
    return result


def _protected_structure(candles: List[Dict]) -> Dict[str, Any]:
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


def _bos_events(candles: List[Dict], side: str, lookback: int = 50) -> List[Dict[str, Any]]:
    """Return confirmed BOS events with their actual candle index.

    A swing is confirmed only after two right candles. The BOS candle must
    close beyond the latest confirmed opposing swing plus a volatility buffer.
    """
    if len(candles) < 10:
        return []
    atr_values = [_safe_atr(candles[:i + 1]) for i in range(len(candles))]
    highs = _swing_highs(candles)
    lows = _swing_lows(candles)
    events: list[Dict[str, Any]] = []
    start = max(0, len(candles) - lookback)
    if side == "LONG":
        for i in range(start, len(candles)):
            candidates = [(idx, price) for idx, price in highs if idx < i]
            if not candidates:
                continue
            swing_idx, level = candidates[-1]
            a = atr_values[i]
            buffer = max(a * BOS_BUFFER_ATR, candles[i]["close"] * BOS_BUFFER_PCT)
            if candles[i]["close"] > level + buffer:
                events.append({"index": i, "time": candles[i]["time"], "level": level, "atr": a, "strength": _bos_strength(candles[i], level, a)})
    else:
        for i in range(start, len(candles)):
            candidates = [(idx, price) for idx, price in lows if idx < i]
            if not candidates:
                continue
            swing_idx, level = candidates[-1]
            a = atr_values[i]
            buffer = max(a * BOS_BUFFER_ATR, candles[i]["close"] * BOS_BUFFER_PCT)
            if candles[i]["close"] < level - buffer:
                events.append({"index": i, "time": candles[i]["time"], "level": level, "atr": a, "strength": _bos_strength(candles[i], level, a)})
    return events


def _bos_strength(candle: Dict, level: float, atr_value: float) -> float:
    rng = max(candle["high"] - candle["low"], 1e-12)
    body = abs(candle["close"] - candle["open"])
    body_ratio = body / rng
    displacement = abs(candle["close"] - level) / atr_value if atr_value > 0 else 0.0
    return min(1.0, 0.5 * min(body_ratio / 0.55, 1.0) + 0.5 * min(displacement / 0.5, 1.0))


def _latest_bos(candles: List[Dict], side: str) -> Optional[Dict[str, Any]]:
    events = _bos_events(candles, side)
    return events[-1] if events else None


def _pullback_retest(candles: List[Dict], side: str, bos: Optional[Dict[str, Any]], max_bars: int = 8) -> Dict[str, Any]:
    """Detect a retest strictly AFTER the BOS candle.

    This fixes the previous engine's critical bug: pre-BOS touches can never
    satisfy the retest condition.
    """
    if not bos:
        return {"valid": False, "index": None, "time": None, "level": None, "quality": 0.0, "rejection": False}
    start = int(bos["index"]) + 1
    end = min(len(candles), start + max_bars)
    if start >= end:
        return {"valid": False, "index": None, "time": None, "level": bos["level"], "quality": 0.0, "rejection": False}
    level = float(bos["level"])
    a = _num(bos.get("atr"), _safe_atr(candles))
    tolerance = max(a * 0.25, level * 0.001)
    best = None
    for i in range(start, end):
        c = candles[i]
        touched = c["low"] <= level + tolerance if side == "LONG" else c["high"] >= level - tolerance
        held = c["close"] > level if side == "LONG" else c["close"] < level
        wick = (min(c["open"], c["close"]) - c["low"]) if side == "LONG" else (c["high"] - max(c["open"], c["close"]))
        rng = max(c["high"] - c["low"], 1e-12)
        rejection = touched and held and wick / rng >= 0.20
        if touched:
            quality = 0.5 + (0.3 if held else 0.0) + (0.2 if rejection else 0.0)
            best = {"valid": bool(held or rejection), "index": i, "time": c["time"], "level": level, "quality": quality, "rejection": rejection}
    return best or {"valid": False, "index": None, "time": None, "level": level, "quality": 0.0, "rejection": False}


def _five_minute_trigger(candles: List[Dict], side: str, setup_level: Optional[float]) -> Dict[str, Any]:
    if len(candles) < 3:
        return {"ready": False, "long": False, "short": False, "quality": 0.0, "rsi": 50.0, "rvol": 0.0, "atr": 0.0, "candle_time": 0}
    c = candles[-1]
    prev = candles[-2]
    close = c["close"]
    rng = max(c["high"] - c["low"], 1e-12)
    body_ratio = abs(c["close"] - c["open"]) / rng
    r = _safe_rsi([x["close"] for x in candles])
    rv = _relative_volume(candles)
    a = _safe_atr(candles)
    long_break = c["close"] > prev["high"]
    short_break = c["close"] < prev["low"]
    long_level = setup_level is None or close > setup_level
    short_level = setup_level is None or close < setup_level
    long_ok = long_break and long_level and r > 50 and rv >= 1.0 and body_ratio >= 0.55
    short_ok = short_break and short_level and r < 50 and rv >= 1.0 and body_ratio >= 0.55
    if side == "LONG":
        q = min(1.0, 0.35 * min(body_ratio / 0.70, 1.0) + 0.35 * min(rv / 1.5, 1.0) + 0.30 * min(max((r - 50) / 15, 0), 1.0))
    elif side == "SHORT":
        q = min(1.0, 0.35 * min(body_ratio / 0.70, 1.0) + 0.35 * min(rv / 1.5, 1.0) + 0.30 * min(max((50 - r) / 15, 0), 1.0))
    else:
        q = 0.0
    return {"ready": bool(long_ok or short_ok), "long": bool(long_ok), "short": bool(short_ok), "quality": q, "rsi": r, "rvol": rv, "atr": a, "candle_time": c["time"], "body_ratio": body_ratio}


def _macd(values: List[float]) -> Tuple[float, float, float]:
    fast = _ema_series(values, 12)
    slow = _ema_series(values, 26)
    if not fast or not slow:
        return 0.0, 0.0, 0.0
    # Align by the most recent common tail.
    n = min(len(fast), len(slow))
    line_series = [fast[-n + i] - slow[-n + i] for i in range(n)]
    signal_series = _ema_series(line_series, 9)
    line = line_series[-1]
    signal = signal_series[-1] if signal_series else 0.0
    return line, signal, line - signal


def _level_clusters(candles: List[Dict], atr_value: float, lookback: int = 100) -> Tuple[Optional[float], Optional[float]]:
    recent = candles[-lookback:] if len(candles) > lookback else candles
    if not recent:
        return None, None
    highs = sorted(c["high"] for c in recent)
    lows = sorted(c["low"] for c in recent)
    current = recent[-1]["close"]
    tol = max(atr_value * 0.20, current * 0.001)
    resistance_candidates = [x for x in highs if x > current + tol]
    support_candidates = [x for x in lows if x < current - tol]
    resistance = min(resistance_candidates) if resistance_candidates else None
    support = max(support_candidates) if support_candidates else None
    return support, resistance


def _target_path(candles_15m: List[Dict], side: str, entry: float, stop: float, atr_value: float) -> Dict[str, Any]:
    risk = abs(entry - stop)
    if risk <= 0:
        return {"ok": False, "tp1": None, "tp2": None, "obstacle": None, "reason": "zero risk"}
    highs = [x[1] for x in _swing_highs(candles_15m)]
    lows = [x[1] for x in _swing_lows(candles_15m)]
    if side == "LONG":
        above = sorted(x for x in highs if x > entry + 0.20 * atr_value)
        tp1 = above[0] if above else entry + 1.2 * risk
        tp2_candidates = [x for x in above if x > entry + 2.0 * risk]
        tp2 = tp2_candidates[0] if tp2_candidates else entry + 2.0 * risk
        obstacle = above[0] if above else None
        ok = tp1 >= entry + 1.2 * risk and (obstacle is None or obstacle > entry + 2.0 * risk)
    else:
        below = sorted((x for x in lows if x < entry - 0.20 * atr_value), reverse=True)
        tp1 = below[0] if below else entry - 1.2 * risk
        tp2_candidates = [x for x in below if x < entry - 2.0 * risk]
        tp2 = tp2_candidates[0] if tp2_candidates else entry - 2.0 * risk
        obstacle = below[0] if below else None
        ok = tp1 <= entry - 1.2 * risk and (obstacle is None or obstacle < entry - 2.0 * risk)
    return {"ok": bool(ok), "tp1": float(tp1), "tp2": float(tp2), "obstacle": obstacle, "risk": risk}


def calculate_trade_levels(data: Dict[str, Any]) -> Dict[str, Any]:
    side = data.get("setup")
    entry = _num(data.get("price"))
    atr15 = _num(data.get("atr"))
    protected_low = data.get("protected_low")
    protected_high = data.get("protected_high")
    if side not in {"LONG", "SHORT"} or entry <= 0 or atr15 <= 0:
        return {"entry": None, "stop_loss": None, "tp1": None, "tp2": None, "rr": None}
    if side == "LONG":
        anchors = [x for x in [protected_low, data.get("support")] if x is not None and _num(x) < entry]
        anchor = max(map(float, anchors)) if anchors else entry - atr15
        stop = anchor - 0.12 * atr15
        risk = entry - stop
        if risk <= 0:
            return {"entry": entry, "stop_loss": None, "tp1": None, "tp2": None, "rr": None}
    else:
        anchors = [x for x in [protected_high, data.get("resistance")] if x is not None and _num(x) > entry]
        anchor = min(map(float, anchors)) if anchors else entry + atr15
        stop = anchor + 0.12 * atr15
        risk = stop - entry
        if risk <= 0:
            return {"entry": entry, "stop_loss": None, "tp1": None, "tp2": None, "rr": None}
    path = _target_path(data.get("_candles_15m", []), side, entry, stop, atr15)
    tp1 = path.get("tp1")
    tp2 = path.get("tp2")
    rr = abs(tp2 - entry) / risk if tp2 is not None else None
    return {"entry": entry, "stop_loss": float(stop), "tp1": tp1, "tp2": tp2, "rr": rr, "target_path_ok": bool(path.get("ok")), "target_obstacle": path.get("obstacle")}


def _build_score(*, direction_ok: bool, structure_ok: bool, setup_ok: bool, momentum_ok: bool, volume_ok: bool, location_ok: bool, futures_ok: bool, volatility_ok: bool, trigger_quality: float = 0.0, rvol: float = 0.0, bos_quality: float = 0.0, retest_quality: float = 0.0) -> Tuple[int, Dict[str, int], int]:
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
    # Evidence quality can only reduce a fully earned group; it never creates a gate.
    if groups["setup_entry_trigger"]:
        q = 0.5 * trigger_quality + 0.25 * bos_quality + 0.25 * retest_quality
        if q < 0.60:
            groups["setup_entry_trigger"] -= 5
    if groups["volume_participation"] and rvol < 1.25:
        groups["volume_participation"] -= 2
    score = max(0, min(100, sum(groups.values())))
    families = sum(bool(x) for x in [direction_ok, structure_ok, setup_ok, momentum_ok, volume_ok, location_ok])
    return score, groups, families


def _data_quality(candles: List[Dict], timeframe_ms: int, minimum: int) -> Tuple[bool, str]:
    if len(candles) < minimum:
        return False, f"not enough candles ({len(candles)}<{minimum})"
    times = [c["time"] for c in candles[-minimum:]]
    if any(b <= a for a, b in zip(times, times[1:])):
        return False, "non-monotonic timestamps"
    # Do not demand perfect history; MEXC can occasionally omit an old candle.
    recent_gap = times[-1] - times[-2]
    if recent_gap > timeframe_ms * 2:
        return False, "recent candle gap"
    return True, "OK"


def analyze_candles(
    symbol: str,
    candles_4h: List,
    candles_1h: List,
    candles_15m: List,
    candles_5m: Optional[List] = None,
    candles_1d: Optional[List] = None,
) -> Dict:
    """Analyze one symbol using only completed candles.

    The function intentionally returns futures_ok=False until scanner.py adds
    real MEXC funding/orderbook/trade-flow context and re-scores the result.
    """
    now_ms = int(time.time() * 1000)
    c4h = closed_candle_rows(convert_candles(candles_4h), TIMEFRAME_MS["4h"], now_ms)
    c1h = closed_candle_rows(convert_candles(candles_1h), TIMEFRAME_MS["1h"], now_ms)
    c15 = closed_candle_rows(convert_candles(candles_15m), TIMEFRAME_MS["15m"], now_ms)
    c5 = closed_candle_rows(convert_candles(candles_5m or []), TIMEFRAME_MS["5m"], now_ms)
    c1d = closed_candle_rows(convert_candles(candles_1d or []), TIMEFRAME_MS["1d"], now_ms)

    for candles, tf, minimum in ((c4h, TIMEFRAME_MS["4h"], 205), (c1h, TIMEFRAME_MS["1h"], 205), (c15, TIMEFRAME_MS["15m"], 80), (c5, TIMEFRAME_MS["5m"], 30)):
        ok, reason = _data_quality(candles, tf, minimum)
        if not ok:
            raise ValueError(f"{symbol}: {reason}")

    close4 = [c["close"] for c in c4h]
    close1 = [c["close"] for c in c1h]
    close15 = [c["close"] for c in c15]
    close5 = [c["close"] for c in c5]
    price = close15[-1]

    # 1D context is contextual only; absence must not manufacture a bias.
    daily_structure = get_structure(c1d) if len(c1d) >= 20 else "UNAVAILABLE"

    # -------------------- 4H regime --------------------
    e21_4 = _safe_ema(close4, 21); e50_4 = _safe_ema(close4, 50); e100_4 = _safe_ema(close4, 100); e200_4 = _safe_ema(close4, 200)
    a4 = _safe_atr(c4h); adx4 = _adx(c4h); slope4 = _ema_slope(close4, 50)
    bull4 = bool(e21_4 and e50_4 and e100_4 and e200_4 and price > e200_4 and e21_4 > e50_4 > e100_4 > e200_4 and slope4 > 0 and adx4 >= 25)
    bear4 = bool(e21_4 and e50_4 and e100_4 and e200_4 and price < e200_4 and e21_4 < e50_4 < e100_4 < e200_4 and slope4 < 0 and adx4 >= 25)
    regime = "BULLISH" if bull4 else "BEARISH" if bear4 else "NO_TRADE"

    # -------------------- 1H direction --------------------
    e21_1 = _safe_ema(close1, 21); e50_1 = _safe_ema(close1, 50)
    structure1 = get_structure(c1h)
    protected1 = _protected_structure(c1h)
    close1_last = close1[-1]
    long1 = bull4 and structure1 == "HH/HL" and e21_1 is not None and e50_1 is not None and close1_last > e50_1 and e21_1 >= e50_1 and protected1["state"] == "BULLISH"
    short1 = bear4 and structure1 == "LH/LL" and e21_1 is not None and e50_1 is not None and close1_last < e50_1 and e21_1 <= e50_1 and protected1["state"] == "BEARISH"

    # -------------------- 15M setup --------------------
    a15 = _safe_atr(c15); r15 = _safe_rsi(close15); rv15 = _relative_volume(c15); vol15 = volume_status(c15)
    bos_long = _latest_bos(c15, "LONG")
    bos_short = _latest_bos(c15, "SHORT")
    ret_long = _pullback_retest(c15, "LONG", bos_long)
    ret_short = _pullback_retest(c15, "SHORT", bos_short)
    long_candidate = long1 and bos_long is not None and ret_long["valid"]
    short_candidate = short1 and bos_short is not None and ret_short["valid"]

    trigger_side = "LONG" if long_candidate else "SHORT" if short_candidate else "NONE"
    trigger_level = bos_long["level"] if trigger_side == "LONG" else bos_short["level"] if trigger_side == "SHORT" else None
    trigger = _five_minute_trigger(c5, trigger_side, trigger_level)
    long_setup = long_candidate and trigger["long"]
    short_setup = short_candidate and trigger["short"]
    setup = "LONG" if long_setup else "SHORT" if short_setup else "NO TRADE"

    # Do not allow a stale BOS/retest to become a fresh signal.
    active_bos = bos_long if setup == "LONG" else bos_short if setup == "SHORT" else None
    active_retest = ret_long if setup == "LONG" else ret_short if setup == "SHORT" else None
    if active_bos and active_retest and active_retest["index"] is not None:
        if len(c15) - 1 - active_retest["index"] > MAX_SETUP_AGE_15M:
            setup = "NO TRADE"
            long_setup = short_setup = False

    support, resistance = _level_clusters(c15, a15)
    try:
        sr_support, sr_resistance = get_support_resistance(c15)
        support = sr_support if sr_support is not None else support
        resistance = sr_resistance if sr_resistance is not None else resistance
    except Exception:
        pass

    # -------------------- volatility / momentum --------------------
    atr_pct = _atr_percent(price, a15)
    atr_pct_rank = _atr_percentile(c15)
    volatility_ok = a15 > 0 and MIN_ATR_PERCENTILE <= atr_pct_rank <= MAX_ATR_PERCENTILE and 0.001 <= atr_pct <= 0.05
    macd_line, macd_signal, macd_hist = _macd(close15)
    momentum_ok = ((setup == "LONG" and 50 < r15 < 75 and macd_hist >= 0) or (setup == "SHORT" and 25 < r15 < 50 and macd_hist <= 0))
    volume_ok = rv15 >= 1.0 and trigger["rvol"] >= 1.0

    # -------------------- trade levels / target path --------------------
    data_for_levels = {
        "setup": setup,
        "price": price,
        "atr": a15,
        "protected_low": protected1.get("protected_low"),
        "protected_high": protected1.get("protected_high"),
        "support": support,
        "resistance": resistance,
        "_candles_15m": c15,
    }
    levels = calculate_trade_levels(data_for_levels)
    sl = levels.get("stop_loss")
    risk = abs(price - sl) if sl is not None else 0.0
    sl_atr = risk / a15 if a15 > 0 else 999.0
    entry_distance = abs(price - (active_retest.get("level") if active_retest else price)) / a15 if a15 > 0 else 0.0
    location_ok = bool(levels.get("target_path_ok")) and entry_distance <= MAX_ENTRY_DISTANCE_ATR
    if setup == "LONG" and resistance is not None and resistance <= price + 1.2 * risk:
        location_ok = False
    if setup == "SHORT" and support is not None and support >= price - 1.2 * risk:
        location_ok = False
    if setup in {"LONG", "SHORT"} and not (MIN_SL_ATR <= sl_atr <= MAX_SL_ATR):
        location_ok = False
    rr = levels.get("rr")
    risk_ok = bool(rr is not None and rr >= MIN_RR and sl is not None and levels.get("tp2") is not None)

    direction_ok = (setup == "LONG" and bull4 and long1) or (setup == "SHORT" and bear4 and short1)
    structure_ok = (setup == "LONG" and structure1 == "HH/HL" and protected1["state"] == "BULLISH" and bos_long is not None and ret_long["valid"]) or (setup == "SHORT" and structure1 == "LH/LL" and protected1["state"] == "BEARISH" and bos_short is not None and ret_short["valid"])
    setup_ok = (setup == "LONG" and long_setup) or (setup == "SHORT" and short_setup)

    # Futures data is deliberately not fabricated here.
    futures_context = "PENDING"
    futures_ok = False

    bullish_points = int(bull4) + int(structure1 == "HH/HL") + int(e21_1 is not None and e50_1 is not None and e21_1 >= e50_1 and close1_last > e50_1) + int(setup == "LONG")
    bearish_points = int(bear4) + int(structure1 == "LH/LL") + int(e21_1 is not None and e50_1 is not None and e21_1 <= e50_1 and close1_last < e50_1) + int(setup == "SHORT")

    score, groups, families = _build_score(
        direction_ok=direction_ok,
        structure_ok=structure_ok,
        setup_ok=setup_ok and risk_ok,
        momentum_ok=momentum_ok,
        volume_ok=volume_ok,
        location_ok=location_ok,
        futures_ok=futures_ok,
        volatility_ok=volatility_ok,
        trigger_quality=_num(trigger.get("quality")),
        rvol=rv15,
        bos_quality=_num((active_bos or {}).get("strength")),
        retest_quality=_num((active_retest or {}).get("quality")),
    )

    reasons: list[str] = []
    if bull4: reasons.append("4H bullish regime")
    if bear4: reasons.append("4H bearish regime")
    if daily_structure != "UNAVAILABLE": reasons.append(f"1D structure {daily_structure}")
    if long1: reasons.append("1H bullish alignment")
    if short1: reasons.append("1H bearish alignment")
    if bos_long: reasons.append("15M bullish BOS confirmed")
    if bos_short: reasons.append("15M bearish BOS confirmed")
    if ret_long["valid"]: reasons.append("15M bullish post-BOS retest")
    if ret_short["valid"]: reasons.append("15M bearish post-BOS retest")
    if trigger["ready"]: reasons.append("5M trigger confirmed")
    if momentum_ok: reasons.append("Momentum aligned")
    if volume_ok: reasons.append("Volume/RVOL aligned")
    if location_ok: reasons.append("Target path and location acceptable")
    if volatility_ok: reasons.append("Volatility percentile acceptable")
    if not futures_ok: reasons.append("Waiting for live MEXC futures context")
    if not risk_ok and setup in {"LONG", "SHORT"}: reasons.append("Risk/RR gate failed")

    result: Dict[str, Any] = {
        "symbol": symbol,
        "price": price,
        "setup": setup,
        "trend_4h": "BULLISH" if bull4 else "BEARISH" if bear4 else "NO_TRADE",
        "regime": regime,
        "daily_structure_1d": daily_structure,
        "structure_1h": structure1,
        "protected_structure_1h": protected1["state"],
        "protected_high": protected1.get("protected_high"),
        "protected_low": protected1.get("protected_low"),
        "bos_15m": bool(bos_long if setup == "LONG" else bos_short if setup == "SHORT" else bos_long or bos_short),
        "bos_15m_time": (active_bos or {}).get("time"),
        "bos_15m_index": (active_bos or {}).get("index"),
        "bos_15m_strength": _num((active_bos or {}).get("strength")),
        "long_bos_level": bos_long["level"] if bos_long else None,
        "short_bos_level": bos_short["level"] if bos_short else None,
        "long_retest": bool(ret_long["valid"]),
        "short_retest": bool(ret_short["valid"]),
        "long_retest_time": ret_long.get("time"),
        "short_retest_time": ret_short.get("time"),
        "ema21": _safe_ema(close15, 21),
        "ema50": _safe_ema(close15, 50),
        "ema21_4h": e21_4, "ema50_4h": e50_4, "ema100_4h": e100_4, "ema200_4h": e200_4,
        "ema21_1h": e21_1, "ema50_1h": e50_1,
        "rsi": r15, "rsi_5m": trigger["rsi"],
        "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
        "atr": a15, "atr_4h": a4, "atr_5m": trigger["atr"], "atr_pct": atr_pct,
        "atr_percentile": atr_pct_rank, "adx_4h": adx4, "ema50_slope_4h": slope4,
        "volume": vol15, "rvol": rv15, "rvol_15m": rv15, "rvol_5m": trigger["rvol"],
        "support": support, "resistance": resistance,
        "futures_context": futures_context, "futures_ok": futures_ok,
        "trigger_quality_5m": trigger["quality"], "trigger_quality": trigger["quality"],
        "five_minute_ready": trigger["ready"], "five_minute_long": trigger["long"], "five_minute_short": trigger["short"],
        "closed_5m_candle_time": trigger["candle_time"],
        "score": score, "score_groups": groups, "confirmation_family_count": families,
        "bullish_points": bullish_points, "bearish_points": bearish_points,
        "direction_ok": direction_ok, "structure_ok": structure_ok, "setup_ok": setup_ok and risk_ok,
        "momentum_ok": momentum_ok, "volume_ok": volume_ok, "location_ok": location_ok,
        "volatility_ok": volatility_ok, "risk_ok": risk_ok,
        "sl_atr": sl_atr, "entry_distance_atr": entry_distance,
        "reasons": reasons,
        "candle_time": c15[-1]["time"],
        "setup_bos_time": (active_bos or {}).get("time"),
        "setup_retest_time": (active_retest or {}).get("time"),
    }
    result.update(levels)

    # Absolute final hard gates. These are intentionally not hidden in score.
    if setup in {"LONG", "SHORT"}:
        if not risk_ok or not location_ok or not volatility_ok or not direction_ok or not structure_ok or not setup_ok:
            result["setup"] = "NO TRADE"
            result["signal_blocked"] = True
        elif score < MIN_SCORE or families < MIN_FAMILIES:
            result["setup"] = "NO TRADE"
            result["signal_blocked"] = True
        else:
            result["signal_blocked"] = False
    else:
        result["signal_blocked"] = True

    return result


async def analyze_symbol(market, symbol: str) -> Dict:
    """Legacy helper retained for compatibility with the existing bot."""
    ref = await market.resolve(symbol)
    c4h = await market.ohlcv(ref, "4H", 250)
    c1h = await market.ohlcv(ref, "1H", 250)
    c15 = await market.ohlcv(ref, "15M", 250)
    c5 = await market.ohlcv(ref, "5M", 250)
    return analyze_candles(ref.symbol, c4h, c1h, c15, c5)
