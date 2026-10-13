from __future__ import annotations

"""Independent, candle-only Smart Money Concepts strategy for 1D/12H/4H/1H.

All structural events are evaluated from completed candles. Swing points carry a
`known_index` internally and are unavailable until their right-side confirmation
bars have closed. This module shares only candle normalization/UTC 12H aggregation
with the legacy engine; its setup, entry, scoring, and risk logic are independent.
"""

from math import isfinite
from typing import Any, Iterable

from .engine import Candle, btc_filter_ok as shared_btc_filter_ok, closed_candle_rows, synthesize_12h_from_4h

ENGINE_VERSION = "SMC-1.0"
MS = {"1H": 3_600_000, "4H": 14_400_000, "12H": 43_200_000, "1D": 86_400_000}
MIN_NET_RR = 1.60
SWING_LEFT = 2
SWING_RIGHT = 2
MAX_SWEEP_AGE = 6


def _f(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for item in values[period:]:
        value = alpha * item + (1.0 - alpha) * value
    return value if isfinite(value) else None


def _atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    ranges: list[float] = []
    for i in range(1, len(candles)):
        row, prev = candles[i], candles[i - 1]
        ranges.append(max(
            float(row["high"]) - float(row["low"]),
            abs(float(row["high"]) - float(prev["close"])),
            abs(float(row["low"]) - float(prev["close"])),
        ))
    sample = ranges[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def _swing_points(candles: list[Candle], left: int = SWING_LEFT, right: int = SWING_RIGHT) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return only pivots knowable at current prefix; known_index is causal."""
    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    for i in range(left, len(candles) - right):
        h, lo = float(candles[i]["high"]), float(candles[i]["low"])
        left_rows, right_rows = candles[i-left:i], candles[i+1:i+right+1]
        if all(h >= float(x["high"]) for x in left_rows) and all(h > float(x["high"]) for x in right_rows):
            highs.append({"index": i, "known_index": i + right, "time": int(candles[i]["time"]), "price": h})
        if all(lo <= float(x["low"]) for x in left_rows) and all(lo < float(x["low"]) for x in right_rows):
            lows.append({"index": i, "known_index": i + right, "time": int(candles[i]["time"]), "price": lo})
    return highs, lows


def _structure(candles: list[Candle]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    highs, lows = _swing_points(candles)
    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN", highs, lows
    hh = highs[-1]["price"] > highs[-2]["price"]
    lh = highs[-1]["price"] < highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    ll = lows[-1]["price"] < lows[-2]["price"]
    if hh and hl:
        return "HH/HL", highs, lows
    if lh and ll:
        return "LH/LL", highs, lows
    return "RANGE", highs, lows


def _regime_1d(candles: list[Candle]) -> dict[str, Any]:
    closes = [float(c["close"]) for c in candles]
    structure, highs, lows = _structure(candles)
    e50, e200 = _ema(closes, 50), _ema(closes, 200)
    price = closes[-1]
    bull = int(structure == "HH/HL") + int(e50 is not None and price > e50) + int(e200 is not None and e50 is not None and e50 > e200)
    bear = int(structure == "LH/LL") + int(e50 is not None and price < e50) + int(e200 is not None and e50 is not None and e50 < e200)
    if bull >= 2 and bull > bear:
        regime = "BULLISH"
    elif bear >= 2 and bear > bull:
        regime = "BEARISH"
    else:
        regime = "RANGE"
    return {"regime": regime, "structure": structure, "bull_votes": bull, "bear_votes": bear,
            "ema50": e50, "ema200": e200, "price": price, "highs": highs, "lows": lows}


def _range_4h(c4: list[Candle]) -> dict[str, float] | None:
    highs, lows = _swing_points(c4)
    # Choose the latest confirmed external high/low from recent structure, not future extrema.
    recent_highs = [p for p in highs if len(c4) - p["known_index"] <= 80]
    recent_lows = [p for p in lows if len(c4) - p["known_index"] <= 80]
    if not recent_highs or not recent_lows:
        return None
    high, low = recent_highs[-1]["price"], recent_lows[-1]["price"]
    if high <= low:
        return None
    return {"high": float(high), "low": float(low), "eq": (float(high) + float(low)) / 2.0}


def _zones(c4: list[Candle], side: str, max_age: int = 40) -> list[dict[str, Any]]:
    """Find objective 3-candle FVGs and BOS-origin order blocks; discard invalidated zones."""
    zones: list[dict[str, Any]] = []
    start = max(2, len(c4) - max_age)
    # Three-candle FVG; later price action is used only to describe current lifecycle.
    for i in range(start, len(c4)):
        a, b = c4[i - 2], c4[i - 1]
        cur = c4[i]
        if side == "LONG" and float(a["high"]) < float(cur["low"]):
            lo, hi, kind = float(a["high"]), float(cur["low"]), "BULLISH_FVG"
        elif side == "SHORT" and float(a["low"]) > float(cur["high"]):
            lo, hi, kind = float(cur["high"]), float(a["low"]), "BEARISH_FVG"
        else:
            continue
        later = c4[i + 1:]
        invalid = any(float(x["close"]) < lo for x in later) if side == "LONG" else any(float(x["close"]) > hi for x in later)
        if invalid:
            continue
        entered = any(float(x["low"]) <= hi and float(x["high"]) >= lo for x in later)
        fully_filled = any(float(x["low"]) <= lo for x in later) if side == "LONG" else any(float(x["high"]) >= hi for x in later)
        status = "INVALIDATED" if invalid else "FULLY_FILLED" if fully_filled else "PARTIALLY_MITIGATED" if entered else "UNTOUCHED"
        zones.append({"low": lo, "high": hi, "kind": kind, "index": i, "time": int(cur["time"]), "status": status})

    # BOS: confirmed swing level, and a later close through it. OB = last opposing candle.
    swing_highs, swing_lows = _swing_points(c4)
    levels = swing_highs if side == "LONG" else swing_lows
    for pivot in levels:
        known = int(pivot["known_index"])
        for j in range(known + 1, len(c4)):
            broke = float(c4[j]["close"]) > float(pivot["price"]) if side == "LONG" else float(c4[j]["close"]) < float(pivot["price"])
            if not broke:
                continue
            origin = None
            for k in range(j - 1, max(-1, j - 7), -1):
                bullish = float(c4[k]["close"]) > float(c4[k]["open"])
                bearish = float(c4[k]["close"]) < float(c4[k]["open"])
                if (side == "LONG" and bearish) or (side == "SHORT" and bullish):
                    origin = k
                    break
            if origin is not None and len(c4) - origin <= max_age:
                lo = float(c4[origin]["low"]) if side == "LONG" else float(c4[origin]["open"])
                hi = float(c4[origin]["open"]) if side == "LONG" else float(c4[origin]["high"])
                if lo > hi:
                    lo, hi = hi, lo
                later = c4[origin + 1:]
                invalid = any(float(x["close"]) < lo for x in later) if side == "LONG" else any(float(x["close"]) > hi for x in later)
                if not invalid:
                    zones.append({"low": lo, "high": hi, "kind": "BULLISH_OB" if side == "LONG" else "BEARISH_OB",
                                  "index": origin, "time": int(c4[origin]["time"]), "status": "MITIGATED" if any(float(x["low"]) <= hi and float(x["high"]) >= lo for x in later) else "UNTOUCHED"})
            break
    # Deduplicate overlapping zones and retain recent zones first.
    zones.sort(key=lambda z: (int(z["index"]), 1 if z["kind"].endswith("FVG") else 0), reverse=True)
    dedup: list[dict[str, Any]] = []
    for z in zones:
        if any(max(float(z["low"]), float(old["low"])) <= min(float(z["high"]), float(old["high"])) and
               min(float(z["high"]), float(old["high"])) - max(float(z["low"]), float(old["low"])) >= 0.5 * min(float(z["high"])-float(z["low"]), float(old["high"])-float(old["low"]))
               for old in dedup):
            continue
        dedup.append(z)
    return dedup[:12]


def _liquidity_trigger(c1: list[Candle], side: str) -> dict[str, Any] | None:
    """Require a prior sweep and a later completed-candle reclaim (never same-bar hindsight)."""
    n = len(c1)
    reclaim_idx = n - 1
    reclaim = c1[reclaim_idx]
    op, hi, lo, close = map(float, (reclaim["open"], reclaim["high"], reclaim["low"], reclaim["close"]))
    span = max(hi - lo, 1e-12)
    body_quality = abs(close - op) / span
    close_location = (close - lo) / span
    if side == "LONG" and not (close > op and close_location >= 0.62 and body_quality >= 0.35):
        return None
    if side == "SHORT" and not (close < op and close_location <= 0.38 and body_quality >= 0.35):
        return None
    earliest = max(3, reclaim_idx - MAX_SWEEP_AGE)
    for sweep_idx in range(reclaim_idx - 1, earliest - 1, -1):
        sweep = c1[sweep_idx]
        before = c1[max(0, sweep_idx - 3):sweep_idx]
        if len(before) < 3:
            continue
        level = min(float(x["low"]) for x in before) if side == "LONG" else max(float(x["high"]) for x in before)
        swept = float(sweep["low"]) < level if side == "LONG" else float(sweep["high"]) > level
        reclaim_ok = close > level and close > float(sweep["high"]) if side == "LONG" else close < level and close < float(sweep["low"])
        if swept and reclaim_ok:
            return {"sweep_idx": sweep_idx, "reclaim_idx": reclaim_idx, "level": level,
                    "sweep_extreme": float(sweep["low"] if side == "LONG" else sweep["high"]),
                    "close_location": close_location, "body_quality": body_quality,
                    "trigger_time": int(reclaim["time"]), "sweep_time": int(sweep["time"])}
    return None


def _net_rr(reward: float, risk: float, entry: float, cost_pct: float) -> float:
    """Conservative reward/risk after estimated round-trip costs."""
    if not isfinite(risk) or risk <= 0 or not isfinite(reward) or not isfinite(entry) or entry <= 0:
        return 0.0
    cost_distance = max(0.0, entry * max(0.0, cost_pct))
    denominator = risk + cost_distance
    return (max(0.0, reward) - cost_distance) / denominator if denominator > 0 else 0.0


def _target_candidates(side: str, entry: float, frames: list[list[Candle]], dealing_range: dict[str, float] | None) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for tf, candles in zip(("4H", "12H", "1D"), frames):
        highs, lows = _swing_points(candles)
        levels = highs if side == "LONG" else lows
        for p in levels:
            price = float(p["price"])
            if (side == "LONG" and price > entry) or (side == "SHORT" and price < entry):
                out.append((price, tf))
    if dealing_range:
        price = dealing_range["high"] if side == "LONG" else dealing_range["low"]
        if (side == "LONG" and price > entry) or (side == "SHORT" and price < entry):
            out.append((price, "4H_RANGE"))
    # Nearest first: don't jump to farther targets to manufacture a better RR.
    out.sort(key=lambda x: abs(x[0] - entry))
    unique: list[tuple[float, str]] = []
    for candidate in out:
        if all(abs(candidate[0] - old[0]) > max(1e-9, entry * 1e-6) for old in unique):
            unique.append(candidate)
    return unique


def _empty(symbol: str, c1d: list[Candle], c12: list[Candle], c4: list[Candle], c1: list[Candle], reason: str, regime: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol.upper(), "price": float(c1[-1]["close"]) if c1 else 0.0,
        "setup": "NO TRADE", "setup_candidate": "NO TRADE", "signal_engine_version": ENGINE_VERSION,
        "signal_basis": "1D regime → 12H context → 4H BOS/OB/FVG/PD location → 1H sweep/reclaim",
        "primary_entry_timeframe": "1H", "setup_timeframe": "4H", "signal_candle_timeframe": "1H",
        "entry_mode": "MARKET", "technical_candidate": False, "direction_ok": False,
        "structure_ok": False, "setup_ok": False, "confirmation_ok": False, "location_ok": False,
        "target_path_structural": False, "structure_quality_ok": False, "shock_veto_ok": True,
        "trade_geometry_ok": False, "risk_ok": False, "rr_ok": False, "volatility_ok": True,
        "confirmation_family_diversity_ok": True, "btc_filter_ok": True, "score": 0,
        "estimated_round_trip_cost_pct": 0.0015, "rejection_stage": "SMC", "primary_rejection_reason": reason,
        "diagnostic_failures": [reason], "technical_gate_failures": [reason],
        "side_diagnostics": [{"side": "LONG", "candidate": False, "primary_failure": reason},
                             {"side": "SHORT", "candidate": False, "primary_failure": reason}],
        "regime_1d": (regime or {}).get("regime", "UNKNOWN"), "closed_1d_candles": len(c1d),
        "closed_12h_candles": len(c12), "closed_4h_candles": len(c4), "closed_1h_candles": len(c1),
        "candle_open_time": int(c1[-1]["time"]) if c1 else 0,
        "candle_close_time": int(c1[-1]["time"]) + MS["1H"] if c1 else 0,
        "candle_time": int(c1[-1]["time"]) + MS["1H"] if c1 else 0,
        "entry_time": int(c1[-1]["time"]) + MS["1H"] if c1 else 0,
    }


def analyze_candles(
    symbol: str, candles_1d: Iterable[Any], candles_12h: Iterable[Any] | None,
    candles_4h: Iterable[Any], candles_1h: Iterable[Any], *, now_ms: int | None = None,
    cache: dict[str, Any] | None = None, btc_context: dict[str, Any] | None = None,
    estimated_round_trip_cost_pct: float = 0.0015,
) -> dict[str, Any]:
    """Analyze one closed-candle snapshot and return the bot's common signal schema."""
    now = int(now_ms or 0) or __import__("time").time_ns() // 1_000_000
    c1d = closed_candle_rows(candles_1d or [], "1D", now)
    c4 = closed_candle_rows(candles_4h or [], "4H", now)
    c1 = closed_candle_rows(candles_1h or [], "1H", now)
    c12 = closed_candle_rows(candles_12h, "12H", now) if candles_12h is not None else synthesize_12h_from_4h(c4, now_ms=now)
    if len(c1d) < 210 or len(c12) < 60 or len(c4) < 180 or len(c1) < 180:
        return _empty(symbol, c1d, c12, c4, c1, f"Insufficient closed data: 1D={len(c1d)},12H={len(c12)},4H={len(c4)},1H={len(c1)}")
    for rows, tf in ((c1d, "1D"), (c12, "12H"), (c4, "4H"), (c1, "1H")):
        times = [int(x["time"]) for x in rows]
        if any(b <= a for a, b in zip(times, times[1:])):
            return _empty(symbol, c1d, c12, c4, c1, f"Non-increasing {tf} timestamps")

    regime = _regime_1d(c1d)
    structure12, highs12, lows12 = _structure(c12)
    structure4, highs4, lows4 = _structure(c4)
    close4, close12 = float(c4[-1]["close"]), float(c12[-1]["close"])
    ema50_12 = _ema([float(x["close"]) for x in c12], 50)
    atr1, atr4 = _atr(c1), _atr(c4)
    dr = _range_4h(c4)
    if regime["regime"] == "RANGE":
        return _empty(symbol, c1d, c12, c4, c1, "1D regime is neutral/ranging; no directional permission", regime)
    side = "LONG" if regime["regime"] == "BULLISH" else "SHORT"
    if structure4 != ("HH/HL" if side == "LONG" else "LH/LL"):
        return _empty(symbol, c1d, c12, c4, c1, f"4H structure {structure4} does not support {side}", regime)
    hostile12 = ((side == "LONG" and structure12 == "LH/LL" and ema50_12 is not None and close12 < ema50_12) or
                 (side == "SHORT" and structure12 == "HH/HL" and ema50_12 is not None and close12 > ema50_12))
    if not dr:
        return _empty(symbol, c1d, c12, c4, c1, "No causally confirmed 4H dealing range", regime)
    current = c1[-1]
    entry = float(current["close"])
    eq = dr["eq"]
    pd_ok = entry <= eq if side == "LONG" else entry >= eq
    zones = _zones(c4, side)
    recent_1h = c1[-8:]
    zone_touched = any(any(float(c["low"]) <= float(z["high"]) and float(c["high"]) >= float(z["low"]) for c in recent_1h)
                       for z in zones if int(z["time"]) <= int(c4[-1]["time"]))
    # An appropriate dealing-range location OR an active POI is sufficient; both improve score.
    if not pd_ok and not zone_touched:
        return _empty(symbol, c1d, c12, c4, c1, f"Price is not in {('discount' if side == 'LONG' else 'premium')} and no recent OB/FVG retest", regime)
    trigger = _liquidity_trigger(c1, side)
    if not trigger:
        return _empty(symbol, c1d, c12, c4, c1, "No completed 1H liquidity sweep followed by later reclaim/displacement", regime)
    range1 = float(current["high"]) - float(current["low"])
    if atr1 <= 0 or atr4 <= 0 or range1 > 4.5 * atr1:
        return _empty(symbol, c1d, c12, c4, c1, "Invalid ATR or 1H shock-candle veto", regime)
    buffer = max(0.20 * atr4, 0.15 * atr1)
    stop = float(trigger["sweep_extreme"]) - buffer if side == "LONG" else float(trigger["sweep_extreme"]) + buffer
    if not (stop < entry if side == "LONG" else stop > entry):
        return _empty(symbol, c1d, c12, c4, c1, "Stop-loss is incorrectly oriented", regime)
    risk = abs(entry - stop)
    target_rows = _target_candidates(side, entry, [c4, c12, c1d], dr)
    if not target_rows:
        return _empty(symbol, c1d, c12, c4, c1, "No confirmed opposing structural target beyond entry", regime)
    target, target_tf = target_rows[0]
    gross_reward = abs(target - entry)
    cost_pct = max(0.0, _f(estimated_round_trip_cost_pct, 0.0015))
    rr_gross = gross_reward / risk if risk > 0 else 0.0
    rr_net = _net_rr(gross_reward, risk, entry, cost_pct)
    if (side == "LONG" and not stop < entry < target) or (side == "SHORT" and not target < entry < stop):
        return _empty(symbol, c1d, c12, c4, c1, "Entry/SL/nearest structural target geometry is invalid", regime)
    if rr_net < MIN_NET_RR:
        return _empty(symbol, c1d, c12, c4, c1, f"Nearest structural target net RR {rr_net:.2f} < {MIN_NET_RR:.2f}", regime)

    nearest_zone = next((z for z in zones if float(z["low"]) <= entry <= float(z["high"])), None)
    btc_ok, btc_reason = shared_btc_filter_ok(side, btc_context or {}, is_btc=symbol.upper().startswith("BTC"))
    points = {
        "htf_bias": 20 if not hostile12 else 8,
        "structure": 15 if structure4 == ("HH/HL" if side == "LONG" else "LH/LL") else 0,
        "liquidity_event": 20,
        "displacement": min(10, int(round(10 * float(trigger["body_quality"]))),),
        "ob_fvg": 10 if nearest_zone or zone_touched else 4,
        "premium_discount": 10 if pd_ok else 5,
        "entry_confirmation": 10,
        "target_quality": 5 if rr_gross >= 2 else 3,
    }
    score = sum(points.values())
    swing_hi = highs4[-1]["price"] if highs4 else dr["high"]
    swing_lo = lows4[-1]["price"] if lows4 else dr["low"]
    return {
        "symbol": symbol.upper(), "price": entry, "setup": side, "setup_candidate": side,
        "signal_engine_version": ENGINE_VERSION,
        "signal_basis": "1D structural regime → 12H context → 4H confirmed BOS/OB/FVG/dealing range → 1H sweep then reclaim",
        "primary_entry_timeframe": "1H", "setup_timeframe": "4H", "signal_candle_timeframe": "1H",
        "entry_mode": "MARKET", "limit_price": None, "entry": entry, "stop_loss": stop, "tp": target,
        "rr": rr_net, "rr_net": rr_net, "rr_gross": rr_gross, "score": score, "score_groups": points,
        "score_hard_gate": False, "estimated_round_trip_cost_pct": cost_pct,
        "direction_ok": True, "structure_ok": True, "setup_ok": True, "confirmation_ok": True,
        "location_ok": True, "target_path_structural": True, "target_path_clear": True,
        "target_path_ok": True, "structure_quality_ok": True, "shock_veto_ok": True,
        "trade_geometry_ok": True, "risk_ok": True, "rr_ok": True, "volatility_ok": True,
        "confirmation_family_diversity_ok": True, "technical_candidate": bool(btc_ok), "signal_blocked": not btc_ok,
        "btc_filter_ok": bool(btc_ok), "btc_filter_reason": btc_reason,
        "regime_1d": regime["regime"], "daily_structure_1d": regime["structure"],
        "bias_12h": "HOSTILE" if hostile12 else "NEUTRAL/ALIGNED", "structure_12h": structure12,
        "structure_4h": structure4, "trend_4h": structure4, "protected_structure_4h": side,
        "premium_discount": "DISCOUNT" if entry < eq else "PREMIUM", "dealing_range_low": dr["low"],
        "dealing_range_high": dr["high"], "equilibrium": eq,
        "poi_kind": nearest_zone["kind"] if nearest_zone else "DEALING_RANGE",
        "poi_low": nearest_zone["low"] if nearest_zone else dr["low"],
        "poi_high": nearest_zone["high"] if nearest_zone else dr["high"],
        "poi_status": nearest_zone["status"] if nearest_zone else "CONTEXT_ONLY",
        "trigger_type": "LIQUIDITY_SWEEP_RECLAIM", "trigger_reason": "confirmed sweep followed by later reclaim/displacement",
        "trigger_side": side, "trigger_quality": trigger["body_quality"], "trigger_quality_1h": trigger["body_quality"],
        "trigger_close_location": trigger["close_location"], "swept_level_1h": trigger["level"],
        "sweep_time_1h": trigger["sweep_time"], "reclaim_time_1h": trigger["trigger_time"],
        "setup_bos_time": int(c4[-1]["time"]), "setup_impulse_high_time": int(c4[-1]["time"]),
        "setup_impulse_low_time": int(c4[-1]["time"]), "target_timeframe": target_tf, "target_levels": [target],
        "stop_source": "1H liquidity-sweep extreme + max(0.20 ATR4H, 0.15 ATR1H)",
        "atr": atr1, "atr_1h": atr1, "atr_4h": atr4,
        "sl_atr": risk / atr4, "sl_atr_4h": risk / atr4, "sl_atr_1h": risk / atr1 if atr1 > 0 else 0,
        "tp_distance_atr": gross_reward / atr4, "tp_distance_atr_1h": gross_reward / atr1 if atr1 > 0 else 0,
        "stop_distance_pct": risk / entry, "tp_distance_pct": gross_reward / entry,
        "rvol": 1.0, "rvol_1h": 1.0, "volume": "NOT_USED_AS_HARD_GATE",
        "entry_1h_ready": True, "momentum_ok": True, "volume_ok": True, "risk_ok": True,
        "target_path_reason": f"Nearest confirmed structural target from {target_tf}",
        "target_path_obstacles": [], "futures_ok": None, "futures_execution_ok": None, "data_fresh": None,
        "futures_context": {"status": "NOT_CHECKED", "execution_ok": None},
        "closed_1d_candles": len(c1d), "closed_12h_candles": len(c12), "closed_4h_candles": len(c4), "closed_1h_candles": len(c1),
        "candle_open_time": int(current["time"]), "candle_close_time": int(current["time"]) + MS["1H"],
        "candle_time": int(current["time"]) + MS["1H"], "entry_time": int(current["time"]) + MS["1H"],
        "intraday_max_hold_minutes": 72 * 60, "side_diagnostics": [
            {"side": "LONG", "candidate": side == "LONG", "primary_failure": None if side == "LONG" else "1D regime chose SHORT"},
            {"side": "SHORT", "candidate": side == "SHORT", "primary_failure": None if side == "SHORT" else "1D regime chose LONG"},
        ], "rejection_stage": None, "primary_rejection_reason": None,
        "diagnostic_failures": [], "technical_gate_failures": [], "reasons": ["Causal SMC setup confirmed"],
        "btc_context": btc_context or {"ok": False, "reason": "not supplied"},
    }


def synthesize_12h_from_4h_smc(candles_4h: Iterable[Any], *, now_ms: int | None = None) -> list[Candle]:
    """Expose the shared, UTC-aligned deterministic aggregator under SMC namespace."""
    return synthesize_12h_from_4h(candles_4h, now_ms=now_ms)
