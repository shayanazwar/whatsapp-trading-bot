from __future__ import annotations

"""Deterministic MEXC Futures multi-timeframe signal engine.

V1.7 - deterministic pipeline.

1D context -> 4H regime -> 1H directional evidence ->
15M BOS/retest -> 5M trigger -> momentum/volume/volatility ->
structural SL -> structural TP path -> RR -> technical candidate.

This module never places orders.
"""

import math
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .indicators import atr, ema, rsi, volume_status
from .structure import get_structure, get_support_resistance

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

MIN_SCORE = 82
MIN_RR = 2.0
MIN_FAMILIES = 5
MIN_SL_ATR = 0.50
MAX_SL_ATR = 2.00
MIN_ATR_PERCENTILE = 10.0
MAX_ATR_PERCENTILE = 98.0
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
ENGINE_VERSION = "gold-v1.8-deterministic"


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
    return [c for c in convert_candles(candles) if int(c["time"]) + interval <= now]


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


def _atr_percentile(candles: List[Candle], period: int = 14, lookback: int = 100) -> float:
    values = _atr_series(candles, period)
    if len(candles) < period + 10:
        return 50.0
    start = max(period, len(candles) - lookback)
    ratios = []
    for i in range(start, len(candles)):
        price, a = float(candles[i]["close"]), values[i]
        if price > 0 and a > 0:
            ratios.append(a / price)
    if not ratios:
        return 50.0
    current = ratios[-1]
    return 100.0 * sum(x <= current for x in ratios) / len(ratios)


def _macd(values: List[float]) -> Tuple[float, float, float]:
    fast, slow = _ema_series(values, 12), _ema_series(values, 26)
    if not fast or not slow:
        return 0.0, 0.0, 0.0
    n = min(len(fast), len(slow))
    line_series = [fast[-n + i] - slow[-n + i] for i in range(n)]
    signal_series = _ema_series(line_series, 9)
    line = line_series[-1]
    signal = signal_series[-1] if signal_series else 0.0
    return line, signal, line - signal


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


def _protected_structure(candles: List[Candle]) -> Dict[str, Any]:
    highs, lows = _swing_highs(candles), _swing_lows(candles)
    ph = highs[-1][1] if highs else None
    pl = lows[-1][1] if lows else None
    if len(highs) < 2 or len(lows) < 2:
        return {"state":"NEUTRAL","protected_high":ph,"protected_low":pl}
    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]
    state = "BULLISH" if h2 > h1 and l2 > l1 else "BEARISH" if h2 < h1 and l2 < l1 else "NEUTRAL"
    return {"state":state,"protected_high":ph,"protected_low":pl}


def _recent_swing_direction(candles: List[Candle], lookback: int = 60) -> Dict[str, Any]:
    sample = candles[-lookback:] if len(candles) > lookback else candles
    highs, lows = _swing_highs(sample), _swing_lows(sample)
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
    protected, swings = _protected_structure(candles), _recent_swing_direction(candles, 80)
    base = {"bull":False,"bear":False,"regime":"NO_TRADE","e21":e21,"e50":e50,
            "e100":e100,"e200":e200,"atr":a,"adx":adx,"slope":slope,
            "protected":protected,"swings":swings,"bull_votes":0,"bear_votes":0}
    if None in (e21,e50,e100,e200) or not close:
        return base
    current = close[-1]

    # Six directional votes. ADX is a trend-strength gate, not a directional vote.
    bull_votes = sum((current > e200, e21 >= e50, e50 >= e100,
                      slope > 0.0, protected["state"] == "BULLISH",
                      swings["bull_score"] >= 1))
    bear_votes = sum((current < e200, e21 <= e50, e50 <= e100,
                      slope < 0.0, protected["state"] == "BEARISH",
                      swings["bear_score"] >= 1))

    bull = bool(current > e200 and e21 >= e50 and adx >= ADX_TREND_MIN and
                bull_votes >= 4 and bull_votes > bear_votes)
    bear = bool(current < e200 and e21 <= e50 and adx >= ADX_TREND_MIN and
                bear_votes >= 4 and bear_votes > bull_votes)
    base.update({"bull":bull,"bear":bear,"regime":"BULLISH" if bull else "BEARISH" if bear else "NO_TRADE",
                 "bull_votes":bull_votes,"bear_votes":bear_votes})
    return base


def _one_hour_alignment(candles: List[Candle], regime4: Dict[str, Any]) -> Dict[str, Any]:
    close = [float(c["close"]) for c in candles]
    price = close[-1]
    e21, e50, e200 = _safe_ema(close,21), _safe_ema(close,50), _safe_ema(close,200)
    structure, protected, swings = get_structure(candles), _protected_structure(candles), _recent_swing_direction(candles,70)
    slope, r, a = _ema_slope(close,50), _safe_rsi(close), _safe_atr(candles)
    base = {"long":False,"short":False,"structure":structure,"protected":protected,"swings":swings,
            "e21":e21,"e50":e50,"e200":e200,"slope":slope,"rsi":r,"atr":a,
            "long_votes":0,"short_votes":0}
    if e21 is None or e50 is None:
        return base
    tolerance = price * EMA_TOLERANCE_PCT

    # Four independent evidence groups. Slope is only counted in GROUP 4.
    long_ema = price >= e50 - tolerance and e21 >= e50
    short_ema = price <= e50 + tolerance and e21 <= e50
    long_structure = structure == "HH/HL" or protected["state"] == "BULLISH" or swings["bull_score"] >= 1
    short_structure = structure == "LH/LL" or protected["state"] == "BEARISH" or swings["bear_score"] >= 1
    long_momentum = r >= 50.0 and (e200 is None or price >= e200 * 0.995)
    short_momentum = r <= 50.0 and (e200 is None or price <= e200 * 1.005)
    # Small counter-slope is allowed so a single noisy 1H candle does not kill alignment.
    long_slope = slope >= -0.0010
    short_slope = slope <= 0.0010

    lv, sv = sum((long_ema,long_structure,long_momentum,long_slope)), sum((short_ema,short_structure,short_momentum,short_slope))
    long = bool(regime4.get("bull") and lv >= 3 and lv > sv)
    short = bool(regime4.get("bear") and sv >= 3 and sv > lv)
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


def _bos_events(candles: List[Candle], side: str, lookback: int = 70) -> List[Dict[str, Any]]:
    if len(candles) < 10 or side not in {"LONG","SHORT"}:
        return []
    atr_values, pivots = _atr_series(candles,14), (_swing_highs(candles) if side=="LONG" else _swing_lows(candles))
    events = []
    start = max(1, len(candles)-lookback)
    for i in range(start,len(candles)):
        a = atr_values[i]
        if a <= 0: continue
        close, prev = float(candles[i]["close"]), float(candles[i-1]["close"])
        buffer = max(a*BOS_BUFFER_ATR, close*BOS_BUFFER_PCT)
        for swing_index, level in reversed([(idx,p) for idx,p in pivots if idx+2 <= i]):
            level = float(level)
            crossed = (prev <= level+buffer and close > level+buffer) if side=="LONG" else (prev >= level-buffer and close < level-buffer)
            if crossed:
                events.append({"index":i,"time":int(candles[i]["time"]),"level":level,"atr":a,
                                "strength":_bos_strength(candles[i],level,a),"swing_index":swing_index})
                break
    return events


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


def _select_latest_bos_with_retest(candles: List[Candle], side: str):
    events = _bos_events(candles,side)
    latest = len(candles)-1
    for bos in reversed(events):
        if latest-int(bos["index"]) > MAX_SETUP_AGE_15M+2: continue
        retest = _pullback_retest(candles,side,bos,MAX_SETUP_AGE_15M)
        if retest["valid"] and latest-int(retest["index"]) <= MAX_SETUP_AGE_15M:
            return bos,retest
    return None,{"valid":False,"index":None,"time":None,"level":None,"quality":0.0,"rejection":False,"low":None,"high":None}


def _five_minute_trigger(candles: List[Candle], side: str, setup_level: Optional[float]) -> Dict[str,Any]:
    empty={"ready":False,"long":False,"short":False,"quality":0.0,"rsi":50.0,"rvol":0.0,"atr":0.0,
           "candle_time":0,"body_ratio":0.0,"trigger_type":"NONE","reason":"insufficient data"}
    if len(candles)<30: return empty
    cur,prev=candles[-1],candles[-2]
    o,h,l,close=float(cur["open"]),float(cur["high"]),float(cur["low"]),float(cur["close"])
    ph,pl=float(prev["high"]),float(prev["low"])
    rng=max(h-l,1e-12)
    body=abs(close-o)/rng
    r,rv,a=_safe_rsi([float(c["close"]) for c in candles]),_relative_volume(candles),_safe_atr(candles)
    long_level=setup_level is None or close>setup_level
    short_level=setup_level is None or close<setup_level
    breakout_long=close>o and close>ph and long_level
    breakout_short=close<o and close<pl and short_level
    reclaim_long=close>o and long_level and (setup_level is None or l<=setup_level)
    reclaim_short=close<o and short_level and (setup_level is None or h>=setup_level)
    mom_long=r>=51 and rv>=MIN_TRIGGER_RVOL and body>=MIN_TRIGGER_BODY
    mom_short=r<=49 and rv>=MIN_TRIGGER_RVOL and body>=MIN_TRIGGER_BODY
    long_ok=(breakout_long or reclaim_long) and mom_long
    short_ok=(breakout_short or reclaim_short) and mom_short
    if side=="LONG":
        ready, q, t = long_ok, _clamp((r-50)/15,0,1), "BREAKOUT" if breakout_long else "RECLAIM" if reclaim_long else "NONE"
    elif side=="SHORT":
        ready, q, t = short_ok, _clamp((50-r)/15,0,1), "BREAKDOWN" if breakout_short else "RECLAIM" if reclaim_short else "NONE"
    else:
        ready,q,t=False,0.0,"NONE"
    quality=_clamp(0.35*_clamp(body/0.70,0,1)+0.30*_clamp(rv/1.50,0,1)+0.35*q,0,1)
    if ready: reason="confirmed"
    elif side=="NONE": reason="not evaluated: no directional 15M setup"
    elif rv<MIN_TRIGGER_RVOL: reason="5M relative volume below threshold"
    elif body<MIN_TRIGGER_BODY: reason="5M candle body too weak"
    elif side=="LONG" and r<51: reason="5M LONG momentum below threshold"
    elif side=="SHORT" and r>49: reason="5M SHORT momentum below threshold"
    else: reason=f"5M {side} breakout/reclaim condition not met"
    return {"ready":bool(ready),"long":bool(long_ok),"short":bool(short_ok),"quality":quality,
            "rsi":r,"rvol":rv,"atr":a,"candle_time":int(cur["time"]),"body_ratio":body,
            "trigger_type":t,"reason":reason}



def _level_clusters(candles: List[Candle], atr_value: float, lookback: int = 120):
    recent = candles[-lookback:] if len(candles) > lookback else candles
    if not recent:
        return None, None
    current = float(recent[-1]["close"])
    tolerance = max(atr_value * 0.20, current * 0.001)
    highs = [float(c["high"]) for c in recent if float(c["high"]) > current + tolerance]
    lows = [float(c["low"]) for c in recent if float(c["low"]) < current - tolerance]
    return (max(lows) if lows else None, min(highs) if highs else None)

def _collect_structural_levels(frames, atr_value: float, entry: float, max_swings_per_frame: int=15):
    raw=[]
    for timeframe,candles in frames:
        if not candles: continue
        for idx,p in _swing_highs(candles)[-max_swings_per_frame:]:
            if p>entry: raw.append({"price":float(p),"timeframe":timeframe,"index":idx,"kind":"RESISTANCE"})
        for idx,p in _swing_lows(candles)[-max_swings_per_frame:]:
            if p<entry: raw.append({"price":float(p),"timeframe":timeframe,"index":idx,"kind":"SUPPORT"})
    if not raw: return []
    tol=max(atr_value*0.15,entry*0.0005)
    raw.sort(key=lambda x:x["price"])
    priority={"1D":4,"4H":3,"1H":2,"15M":1}
    clusters=[]
    for level in raw:
        if not clusters or abs(level["price"]-clusters[-1]["price"])>tol:
            clusters.append(level.copy())
        elif priority.get(level["timeframe"],0)>priority.get(clusters[-1]["timeframe"],0):
            clusters[-1]=level.copy()
    return clusters


def _target_path(frames, side: str, entry: float, stop: float, atr_value: float):
    risk=abs(entry-stop)
    base={"ok":False,"tp1":None,"tp2":None,"obstacle":None,"risk":risk,"structural":False}
    if risk<=0 or atr_value<=0:
        base["reason"]="zero risk or ATR"; return base
    levels=_collect_structural_levels(frames,atr_value,entry)
    clearance=max(0.10*atr_value,entry*0.0005)
    ordered=[x for x in levels if (x["price"]>entry+clearance if side=="LONG" else x["price"]<entry-clearance)]
    ordered.sort(key=lambda x:x["price"],reverse=side=="SHORT")
    if not ordered:
        base["reason"]="no confirmed structural target"; return base
    min1,min2=MIN_TP1_R*risk,MIN_TP2_R*risk
    tp1_level=next((x for x in ordered if ((x["price"]-entry) if side=="LONG" else (entry-x["price"]))>=min1),None)
    if not tp1_level:
        base.update({"tp1":ordered[0]["price"],"obstacle":ordered[0]["price"],"reason":"nearest structural target is closer than 1.20R","structural":True,"target_levels":ordered[:8]})
        return base
    tp1=tp1_level["price"]
    tp2_level=next((x for x in ordered if ((x["price"]>tp1+clearance) if side=="LONG" else (x["price"]<tp1-clearance)) and (((x["price"]-entry) if side=="LONG" else (entry-x["price"]))>=min2)),None)
    if not tp2_level:
        # TP2-only fix: if the nearest confirmed structural target already
        # reaches 2R, keep that real structural level as TP2 and use 1.20R
        # as the partial-profit TP1. No synthetic structural TP2 is created.
        first_distance = ((ordered[0]["price"] - entry) if side == "LONG" else (entry - ordered[0]["price"]))
        if first_distance >= min2:
            tp1_fallback = entry + min1 if side == "LONG" else entry - min1
            base.update({
                "ok":True,
                "tp1":tp1_fallback,
                "tp2":ordered[0]["price"],
                "reason":"single structural target beyond 2.00R; TP1 at 1.20R and TP2 at confirmed structural target",
                "structural":True,
                "tp1_level":None,
                "tp2_level":ordered[0],
                "target_levels":ordered[:8],
            })
            return base
        base.update({"tp1":tp1,"reason":"no second structural target reaches 2.00R","structural":True,"tp1_level":tp1_level,"target_levels":ordered[:8]})
        return base
    base.update({"ok":True,"tp1":tp1,"tp2":tp2_level["price"],"reason":"two distinct structural targets","structural":True,
                 "tp1_level":tp1_level,"tp2_level":tp2_level,"target_levels":ordered[:8]})
    return base


def calculate_trade_levels(data: Dict[str,Any]) -> Dict[str,Any]:
    side=str(data.get("setup") or "").upper()
    entry=_num(data.get("price")); a=_num(data.get("atr"))
    empty={"entry":entry if entry>0 else None,"stop_loss":None,"tp1":None,"tp2":None,"rr":None,
           "target_path_ok":False,"target_path_structural":False}
    if side not in {"LONG","SHORT"} or entry<=0 or a<=0: return empty
    retest=data.get("retest") or {}
    if side=="LONG":
        anchors=[retest.get("low"),data.get("support"),data.get("protected_low")]
        vals=[_num(x) for x in anchors if x is not None and _num(x)>0 and _num(x)<entry]
        anchor=max(vals) if vals else entry-a
        stop=anchor-0.12*a
    else:
        anchors=[retest.get("high"),data.get("resistance"),data.get("protected_high")]
        vals=[_num(x) for x in anchors if x is not None and _num(x)>entry]
        anchor=min(vals) if vals else entry+a
        stop=anchor+0.12*a
    if (side=="LONG" and stop>=entry) or (side=="SHORT" and stop<=entry):
        return empty | {"entry":entry}
    max_stop=MAX_SL_ATR*a
    if abs(entry-stop)>max_stop:
        stop=entry-max_stop if side=="LONG" else entry+max_stop
    frames=data.get("target_frames") or [("15M",data.get("_candles_15m",[]))]
    path=_target_path(frames,side,entry,stop,a)
    tp1,tp2=path.get("tp1"),path.get("tp2")
    risk=abs(entry-stop)
    rr=abs(tp2-entry)/risk if tp2 is not None and risk>0 else None
    return {"entry":entry,"stop_loss":float(stop),"tp1":float(tp1) if tp1 is not None else None,
            "tp2":float(tp2) if tp2 is not None else None,"rr":rr,
            "target_path_ok":bool(path.get("ok")),"target_path_structural":bool(path.get("structural")),
            "target_obstacle":path.get("obstacle"),"target_path_reason":path.get("reason"),
            "target_levels":path.get("target_levels",[])}


def _build_score(*,direction_ok,structure_ok,setup_ok,momentum_ok,volume_ok,location_ok,
                 futures_ok,volatility_ok,trigger_quality=0,rvol=0,bos_quality=0,retest_quality=0):
    groups={"direction_regime":20 if direction_ok else 0,"market_structure":20 if structure_ok else 0,
            "setup_entry_trigger":20 if setup_ok else 0,"momentum":10 if momentum_ok else 0,
            "volume_participation":10 if volume_ok else 0,"location_target_path":10 if location_ok else 0,
            "futures_market_context":5 if futures_ok else 0,"volatility_execution":5 if volatility_ok else 0}
    if groups["setup_entry_trigger"]:
        quality=.50*trigger_quality+.25*bos_quality+.25*retest_quality
        if quality<.60: groups["setup_entry_trigger"]-=5
    if groups["volume_participation"] and rvol<1.25:
        groups["volume_participation"]-=2
    families=sum(bool(x) for x in (direction_ok,structure_ok,setup_ok,momentum_ok,volume_ok,location_ok))
    return max(0,min(100,sum(groups.values()))),groups,families


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


def _diagnostic_failures(regime, alignment, long_candidate, short_candidate, bos_long, bos_short,
                         ret_long, ret_short, trigger_side, trigger, setup, momentum_ok, volume_ok,
                         location_ok, risk_ok, volatility_ok, score, families):
    failures=[]
    if not (regime["bull"] or regime["bear"]): failures.append("4H regime")
    if not (alignment["long"] or alignment["short"]): failures.append("1H alignment")
    directional_15m = long_candidate or short_candidate
    if not directional_15m:
        if not bos_long and not bos_short:
            failures.append("15M BOS")
        elif not ret_long["valid"] and not ret_short["valid"]:
            failures.append("15M post-BOS retest")
        elif alignment["long"] or alignment["short"]:
            failures.append("15M directional setup")
    if trigger_side in {"LONG","SHORT"} and not trigger.get("ready"):
        failures.append("5M trigger")
    if setup in {"LONG","SHORT"}:
        if not momentum_ok: failures.append("momentum")
        if not location_ok: failures.append("target path/location")
        if not risk_ok: failures.append("risk/RR")
        if not volatility_ok: failures.append("volatility")
        if score<MIN_SCORE: failures.append("score")
        if families<MIN_FAMILIES: failures.append("confirmation families")
    if not failures:
        # Deterministic fallback: explain the first stage that stopped the pipeline.
        if not (regime["bull"] or regime["bear"]): failures.append("4H regime")
        elif not (alignment["long"] or alignment["short"]): failures.append("1H alignment")
        elif not directional_15m: failures.append("15M directional setup")
        elif trigger_side in {"LONG","SHORT"} and not trigger.get("ready"): failures.append("5M trigger")
        else: failures.append("technical hard gate")
    return list(dict.fromkeys(failures))


def analyze_candles(symbol: str, candles_4h: List, candles_1h: List, candles_15m: List,
                    candles_5m: Optional[List]=None, candles_1d: Optional[List]=None) -> Dict[str,Any]:
    now=int(time.time()*1000)
    c4=closed_candle_rows(candles_4h,"4h",now); c1=closed_candle_rows(candles_1h,"1h",now)
    c15=closed_candle_rows(candles_15m,"15m",now); c5=closed_candle_rows(candles_5m or [],"5m",now)
    c1d=closed_candle_rows(candles_1d or [],"1d",now)
    for candles,tf,n in ((c4,"4h",205),(c1,"1h",205),(c15,"15m",80),(c5,"5m",30)):
        ok,reason=_data_quality(candles,TIMEFRAME_MS[tf],n)
        if not ok: raise ValueError(f"{symbol}: {reason}")

    close4=[float(c["close"]) for c in c4]; close1=[float(c["close"]) for c in c1]; close15=[float(c["close"]) for c in c15]
    price=close15[-1]
    regime=_four_hour_regime(c4); alignment=_one_hour_alignment(c1,regime)
    protected=alignment["protected"]; structure1=alignment["structure"]
    e21_1,e50_1=alignment["e21"],alignment["e50"]
    atr15=_safe_atr(c15); r15=_safe_rsi(close15); rv15=_relative_volume(c15); vol15=volume_status(c15)

    bos_long,ret_long=_select_latest_bos_with_retest(c15,"LONG")
    bos_short,ret_short=_select_latest_bos_with_retest(c15,"SHORT")
    long_candidate=bool(alignment["long"] and bos_long and ret_long["valid"])
    short_candidate=bool(alignment["short"] and bos_short and ret_short["valid"])
    if long_candidate and not short_candidate: trigger_side="LONG"
    elif short_candidate and not long_candidate: trigger_side="SHORT"
    elif long_candidate and short_candidate:
        trigger_side="LONG" if _num((bos_long or {}).get("strength")) >= _num((bos_short or {}).get("strength")) else "SHORT"
    else: trigger_side="NONE"

    active_bos=bos_long if trigger_side=="LONG" else bos_short if trigger_side=="SHORT" else None
    active_retest=ret_long if trigger_side=="LONG" else ret_short if trigger_side=="SHORT" else None
    trigger_level=float(active_bos["level"]) if active_bos else None

    trigger=_five_minute_trigger(c5,trigger_side,trigger_level)
    if active_retest and trigger["ready"] and trigger["candle_time"] < int(active_retest["time"]):
        trigger=dict(trigger); trigger.update({"ready":False,"long":False,"short":False,"trigger_type":"INVALID_BEFORE_RETEST",
                                              "reason":"5M trigger occurred before 15M retest"})
    setup="LONG" if trigger_side=="LONG" and trigger["long"] else "SHORT" if trigger_side=="SHORT" and trigger["short"] else "NO TRADE"
    if setup in {"LONG","SHORT"} and active_retest and trigger["candle_time"]-int(active_retest["time"])>30*60*1000:
        setup="NO TRADE"; trigger=dict(trigger); trigger.update({"ready":False,"long":False,"short":False,
                                                                  "reason":"5M trigger is too old for the active 15M retest"})

    support,resistance=_level_clusters(c15,atr15)
    try:
        sr_support,sr_resistance=get_support_resistance(c15)
        if sr_support is not None: support=sr_support
        if sr_resistance is not None: resistance=sr_resistance
    except Exception: pass

    atr_pct=_atr_percent(price,atr15); atr_rank=_atr_percentile(c15)
    volatility_ok=bool(atr15>0 and MIN_ATR_PERCENTILE<=atr_rank<=MAX_ATR_PERCENTILE and 0.0005<=atr_pct<=0.05)
    macd_line,macd_signal,macd_hist=_macd(close15)
    momentum_ok=bool((setup=="LONG" and 50<r15<78 and macd_hist>=0) or (setup=="SHORT" and 22<r15<50 and macd_hist<=0))
    # 15M volume/RVOL is NOT a hard rejection gate.
    # The 5M trigger already requires minimum RVOL, so confirmed entries
    # retain volume participation without cancelling a good structure setup
    # because the 15M RVOL is temporarily weak.
    volume_ok=bool(trigger["rvol"]>=MIN_TRIGGER_RVOL)

    levels=calculate_trade_levels({"setup":setup,"price":price,"atr":atr15,
        "protected_low":protected.get("protected_low"),"protected_high":protected.get("protected_high"),
        "support":support,"resistance":resistance,"retest":active_retest or {},
        "target_frames":[("1D",c1d),("4H",c4),("1H",c1),("15M",c15)],"_candles_15m":c15})
    sl=levels.get("stop_loss"); risk=abs(price-sl) if sl is not None else 0.0
    sl_atr=risk/atr15 if atr15>0 else 999.0
    entry_distance=(abs(price-float(active_retest["level"]))/atr15 if active_retest and active_retest.get("level") is not None and atr15>0 else 0.0)
    location_ok=bool(levels.get("target_path_ok") and levels.get("target_path_structural") and entry_distance<=MAX_ENTRY_DISTANCE_ATR)
    if setup in {"LONG","SHORT"} and not MIN_SL_ATR<=sl_atr<=MAX_SL_ATR: location_ok=False
    rr=levels.get("rr")
    risk_ok=bool(setup in {"LONG","SHORT"} and sl is not None and levels.get("tp2") is not None and rr is not None and rr>=MIN_RR)

    direction_ok=bool((setup=="LONG" and alignment["long"] and regime["bull"]) or (setup=="SHORT" and alignment["short"] and regime["bear"]))
    structure_ok=bool((setup=="LONG" and bos_long and ret_long["valid"]) or (setup=="SHORT" and bos_short and ret_short["valid"]))
    setup_ok=bool(((setup=="LONG" and trigger["long"] and long_candidate) or (setup=="SHORT" and trigger["short"] and short_candidate)) and risk_ok)
    score,groups,families=_build_score(direction_ok=direction_ok,structure_ok=structure_ok,setup_ok=setup_ok,
        momentum_ok=momentum_ok,volume_ok=volume_ok,location_ok=location_ok,futures_ok=False,volatility_ok=volatility_ok,
        trigger_quality=_num(trigger.get("quality")),rvol=rv15,bos_quality=_num((active_bos or {}).get("strength")),
        retest_quality=_num((active_retest or {}).get("quality")))
    technical_candidate=bool(setup in {"LONG","SHORT"} and direction_ok and structure_ok and setup_ok and momentum_ok and
                              location_ok and volatility_ok and risk_ok and rr is not None and rr>=MIN_RR and
                              score>=MIN_SCORE and families>=MIN_FAMILIES)
    failures=_diagnostic_failures(regime,alignment,long_candidate,short_candidate,bos_long,bos_short,ret_long,ret_short,
                                  trigger_side,trigger,setup,momentum_ok,volume_ok,location_ok,risk_ok,volatility_ok,score,families)

    reasons=[]
    if regime["bull"]: reasons.append("4H bullish regime")
    if regime["bear"]: reasons.append("4H bearish regime")
    if len(c1d)>=20:
        try:
            ds=get_structure(c1d)
            if ds!="UNKNOWN": reasons.append(f"1D structure {ds}")
        except Exception: pass
    if alignment["long"]: reasons.append(f"1H bullish alignment ({alignment['long_votes']}/4)")
    if alignment["short"]: reasons.append(f"1H bearish alignment ({alignment['short_votes']}/4)")
    if active_bos: reasons.append(f"15M {trigger_side} BOS confirmed")
    if active_retest and active_retest.get("valid"): reasons.append(f"15M {trigger_side} retest confirmed")
    if trigger.get("ready"): reasons.append(f"5M {trigger.get('trigger_type','TRIGGER')} confirmed")
    if momentum_ok: reasons.append("Momentum aligned")
    if volume_ok: reasons.append("Volume/RVOL aligned")
    if location_ok: reasons.append("Structural target path acceptable")
    if risk_ok and rr is not None: reasons.append(f"Risk acceptable ({rr:.2f}R)")
    if volatility_ok: reasons.append("Volatility acceptable")
    if not technical_candidate: reasons.append("Technical hard gate failed")

    ema21_15,ema50_15=_safe_ema(close15,21),_safe_ema(close15,50)
    daily_structure=get_structure(c1d) if len(c1d)>=20 else "UNAVAILABLE"
    ema_direction="BULLISH" if (ema21_15 or 0)>(ema50_15 or 0) else "BEARISH" if (ema21_15 or 0)<(ema50_15 or 0) else "NEUTRAL"
    return {
        "symbol":symbol,"price":price,"setup":setup,"setup_candidate":setup if setup in {"LONG","SHORT"} else "NO TRADE",
        "trend_4h":"BULLISH" if regime["bull"] else "BEARISH" if regime["bear"] else "NO_TRADE","regime":regime["regime"],
        "daily_structure_1d":daily_structure,"structure_1h":structure1,"protected_structure_1h":protected["state"],
        "protected_high":protected.get("protected_high"),"protected_low":protected.get("protected_low"),
        "one_hour_long_votes":alignment["long_votes"],"one_hour_short_votes":alignment["short_votes"],
        "one_hour_evidence":{k:alignment.get(k,False) for k in ("long_ema","short_ema","long_structure","short_structure","long_momentum","short_momentum","long_slope","short_slope")},
        "bos_15m":bool(active_bos),"bos_15m_time":active_bos.get("time") if active_bos else None,
        "bos_15m_index":active_bos.get("index") if active_bos else None,"bos_15m_strength":_num((active_bos or {}).get("strength")),
        "long_bos_level":bos_long.get("level") if bos_long else None,"short_bos_level":bos_short.get("level") if bos_short else None,
        "long_bos_event_count":len(_bos_events(c15,"LONG")),"short_bos_event_count":len(_bos_events(c15,"SHORT")),
        "long_retest":bool(ret_long.get("valid")),"short_retest":bool(ret_short.get("valid")),
        "long_retest_time":ret_long.get("time"),"short_retest_time":ret_short.get("time"),"retest":active_retest or {},
        "ema21":ema21_15,"ema50":ema50_15,"ema21_4h":regime["e21"],"ema50_4h":regime["e50"],"ema100_4h":regime["e100"],"ema200_4h":regime["e200"],
        "ema21_1h":e21_1,"ema50_1h":e50_1,"ema200_1h":alignment["e200"],"ema_direction":ema_direction,
        "rsi":r15,"rsi_5m":trigger["rsi"],"macd":macd_line,"macd_signal":macd_signal,"macd_hist":macd_hist,
        "atr":atr15,"atr_4h":regime["atr"],"atr_5m":trigger["atr"],"atr_pct":atr_pct,"atr_percentile":atr_rank,
        "adx_4h":regime["adx"],"ema50_slope_4h":regime["slope"],"volume":vol15,"rvol":rv15,"rvol_15m":rv15,"rvol_5m":trigger["rvol"],
        "support":support,"resistance":resistance,"futures_context":"PENDING","futures_ok":False,
        "btc_filter_ok":False,"btc_filter_reason":"PENDING","data_fresh":True,"signal_engine_version":ENGINE_VERSION,
        "trigger_side":trigger_side,"trigger_5m":"CONFIRMED" if trigger.get("ready") else "NONE",
        "trigger_type_5m":trigger.get("trigger_type","NONE"),"trigger_reason_5m":trigger.get("reason","unknown"),
        "trigger_quality_5m":trigger["quality"],"trigger_quality":trigger["quality"],"five_minute_ready":bool(trigger["ready"]),
        "five_minute_long":bool(trigger["long"]),"five_minute_short":bool(trigger["short"]),
        "closed_5m_candle_time":trigger["candle_time"],"score":score,"score_groups":groups,
        "confirmation_family_count":families,"bullish_points":int(regime["bull"])+int(alignment["long"])+int(e21_1 is not None and e50_1 is not None and e21_1>=e50_1),
        "bearish_points":int(regime["bear"])+int(alignment["short"])+int(e21_1 is not None and e50_1 is not None and e21_1<=e50_1),
        "direction_ok":direction_ok,"structure_ok":structure_ok,"setup_ok":setup_ok,"momentum_ok":momentum_ok,"volume_ok":volume_ok,
        "location_ok":location_ok,"volatility_ok":volatility_ok,"risk_ok":risk_ok,"sl_atr":sl_atr,"entry_distance_atr":entry_distance,
        "target_path_ok":bool(levels.get("target_path_ok")),"target_path_structural":bool(levels.get("target_path_structural")),
        "target_path_reason":levels.get("target_path_reason"),"target_obstacle":levels.get("target_obstacle"),
        "technical_candidate":technical_candidate,"signal_blocked":not technical_candidate,
        "rejection_stage":None if technical_candidate else "TECHNICAL","technical_gate_failures":failures,"reasons":reasons,
        "candle_time":int(c15[-1]["time"]),"setup_bos_time":active_bos.get("time") if active_bos else None,
        "setup_retest_time":active_retest.get("time") if active_retest else None,**levels,
    }


async def analyze_symbol(market, symbol: str) -> Dict[str,Any]:
    ref=await market.resolve(symbol)
    c1d=await market.ohlcv(ref,"1D",100); c4h=await market.ohlcv(ref,"4H",250)
    c1h=await market.ohlcv(ref,"1H",250); c15=await market.ohlcv(ref,"15M",250); c5=await market.ohlcv(ref,"5M",250)
    return analyze_candles(ref.symbol,c4h,c1h,c15,c5,c1d)
