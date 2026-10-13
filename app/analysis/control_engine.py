from __future__ import annotations

"""Simple trend/breakout control strategy for matched offline comparisons.

This is intentionally not an SMC model: daily EMA trend + 4H trend alignment +
a completed 1H three-bar breakout, with structural risk and HTF swing targets.
It exists as a paper/backtest comparator, not as a performance claim.
"""

from typing import Any, Iterable

from .engine import Candle, btc_filter_ok, closed_candle_rows, synthesize_12h_from_4h
from .smc_engine import MIN_NET_RR, _atr, _ema, _net_rr, _swing_points

ENGINE_VERSION = "CONTROL-1.0"
HOUR_MS = 3_600_000


def _empty(symbol: str, c1d: list[Candle], c12: list[Candle], c4: list[Candle], c1: list[Candle], reason: str, regime: str = "UNKNOWN") -> dict[str, Any]:
    return {
        "symbol": symbol.upper(), "price": float(c1[-1]["close"]) if c1 else 0.0,
        "setup": "NO TRADE", "setup_candidate": "NO TRADE", "signal_engine_version": ENGINE_VERSION,
        "signal_basis": "1D EMA trend → 4H EMA trend → 1H three-bar breakout control",
        "primary_entry_timeframe": "1H", "setup_timeframe": "4H", "signal_candle_timeframe": "1H",
        "entry_mode": "MARKET", "technical_candidate": False, "direction_ok": False,
        "structure_ok": False, "setup_ok": False, "confirmation_ok": False, "location_ok": False,
        "target_path_structural": False, "target_path_clear": False, "structure_quality_ok": True,
        "shock_veto_ok": True, "trade_geometry_ok": False, "risk_ok": False, "rr_ok": False,
        "volatility_ok": True, "confirmation_family_diversity_ok": True, "btc_filter_ok": True,
        "score": 0, "estimated_round_trip_cost_pct": 0.0015, "rejection_stage": "CONTROL",
        "primary_rejection_reason": reason, "diagnostic_failures": [reason], "technical_gate_failures": [reason],
        "side_diagnostics": [{"side": "LONG", "candidate": False, "primary_failure": reason},
                             {"side": "SHORT", "candidate": False, "primary_failure": reason}],
        "regime_1d": regime, "closed_1d_candles": len(c1d), "closed_12h_candles": len(c12),
        "closed_4h_candles": len(c4), "closed_1h_candles": len(c1),
        "candle_open_time": int(c1[-1]["time"]) if c1 else 0,
        "candle_close_time": int(c1[-1]["time"]) + HOUR_MS if c1 else 0,
        "candle_time": int(c1[-1]["time"]) + HOUR_MS if c1 else 0,
        "entry_time": int(c1[-1]["time"]) + HOUR_MS if c1 else 0,
    }


def analyze_candles(
    symbol: str, candles_1d: Iterable[Any], candles_12h: Iterable[Any] | None,
    candles_4h: Iterable[Any], candles_1h: Iterable[Any], *, now_ms: int | None = None,
    cache: dict[str, Any] | None = None, btc_context: dict[str, Any] | None = None,
    estimated_round_trip_cost_pct: float = 0.0015,
) -> dict[str, Any]:
    now = int(now_ms or 0) or __import__("time").time_ns() // 1_000_000
    c1d = closed_candle_rows(candles_1d or [], "1D", now)
    c4 = closed_candle_rows(candles_4h or [], "4H", now)
    c1 = closed_candle_rows(candles_1h or [], "1H", now)
    c12 = closed_candle_rows(candles_12h, "12H", now) if candles_12h is not None else synthesize_12h_from_4h(c4, now_ms=now)
    if len(c1d) < 210 or len(c12) < 60 or len(c4) < 180 or len(c1) < 180:
        return _empty(symbol, c1d, c12, c4, c1, f"Insufficient closed data: 1D={len(c1d)},12H={len(c12)},4H={len(c4)},1H={len(c1)}")

    daily_closes = [float(x["close"]) for x in c1d]
    h4_closes = [float(x["close"]) for x in c4]
    e50d, e200d = _ema(daily_closes, 50), _ema(daily_closes, 200)
    e21_4, e50_4 = _ema(h4_closes, 21), _ema(h4_closes, 50)
    if e50d is None or e200d is None or e21_4 is None or e50_4 is None:
        return _empty(symbol, c1d, c12, c4, c1, "Insufficient EMA warmup")
    price_d, price4 = daily_closes[-1], h4_closes[-1]
    bull = price_d > e50d and e50d > e200d and price4 > e50_4 and e21_4 > e50_4
    bear = price_d < e50d and e50d < e200d and price4 < e50_4 and e21_4 < e50_4
    if not bull and not bear:
        return _empty(symbol, c1d, c12, c4, c1, "Daily and 4H EMA trend do not align", "RANGE")
    side = "LONG" if bull else "SHORT"

    prior = c1[-4:-1]
    current = c1[-1]
    if len(prior) != 3:
        return _empty(symbol, c1d, c12, c4, c1, "Not enough 1H breakout candles", side)
    level = max(float(x["high"]) for x in prior) if side == "LONG" else min(float(x["low"]) for x in prior)
    previous_close = float(c1[-2]["close"])
    entry = float(current["close"])
    crossed = entry > level and previous_close <= level if side == "LONG" else entry < level and previous_close >= level
    if not crossed:
        return _empty(symbol, c1d, c12, c4, c1, "No fresh completed 1H three-bar breakout", "BULLISH" if bull else "BEARISH")

    atr1, atr4 = _atr(c1), _atr(c4)
    if atr1 <= 0 or atr4 <= 0 or float(current["high"]) - float(current["low"]) > 4.5 * atr1:
        return _empty(symbol, c1d, c12, c4, c1, "Invalid ATR or 1H shock-candle veto", "BULLISH" if bull else "BEARISH")
    buffer = max(0.20 * atr4, 0.15 * atr1)
    prior_risk_candles = c1[-6:-1]
    if side == "LONG":
        stop = min(float(x["low"]) for x in prior_risk_candles) - buffer
    else:
        stop = max(float(x["high"]) for x in prior_risk_candles) + buffer
    if not (stop < entry if side == "LONG" else stop > entry):
        return _empty(symbol, c1d, c12, c4, c1, "Control stop has invalid orientation", "BULLISH" if bull else "BEARISH")
    risk = abs(entry - stop)

    targets: list[tuple[float, str]] = []
    for timeframe, frame in (("4H", c4), ("12H", c12), ("1D", c1d)):
        highs, lows = _swing_points(frame)
        for point in highs if side == "LONG" else lows:
            value = float(point["price"])
            if (side == "LONG" and value > entry) or (side == "SHORT" and value < entry):
                targets.append((value, timeframe))
    targets.sort(key=lambda item: abs(item[0] - entry))
    if not targets:
        return _empty(symbol, c1d, c12, c4, c1, "No opposing confirmed HTF swing target", "BULLISH" if bull else "BEARISH")
    target, target_tf = targets[0]
    gross_reward = abs(target - entry)
    cost_pct = max(0.0, float(estimated_round_trip_cost_pct or 0.0))
    rr_gross = gross_reward / risk if risk > 0 else 0.0
    rr_net = _net_rr(gross_reward, risk, entry, cost_pct)
    if rr_net < MIN_NET_RR:
        return _empty(symbol, c1d, c12, c4, c1, f"Nearest target net RR {rr_net:.2f} < {MIN_NET_RR:.2f}", "BULLISH" if bull else "BEARISH")

    btc_ok, btc_reason = btc_filter_ok(side, btc_context or {}, is_btc=symbol.upper().startswith("BTC"))
    return {
        "symbol": symbol.upper(), "price": entry, "setup": side, "setup_candidate": side,
        "signal_engine_version": ENGINE_VERSION,
        "signal_basis": "1D EMA50/200 trend → 4H EMA21/50 alignment → completed 1H three-bar breakout",
        "primary_entry_timeframe": "1H", "setup_timeframe": "4H", "signal_candle_timeframe": "1H",
        "entry_mode": "MARKET", "entry": entry, "stop_loss": stop, "tp": target, "rr": rr_net,
        "rr_net": rr_net, "rr_gross": rr_gross, "estimated_round_trip_cost_pct": cost_pct,
        "score": 50, "score_hard_gate": False, "score_groups": {"daily_trend": 20, "4h_trend": 15, "1h_breakout": 10, "target_geometry": 5},
        "direction_ok": True, "structure_ok": True, "setup_ok": True, "confirmation_ok": True,
        "location_ok": True, "target_path_structural": True, "target_path_clear": True,
        "structure_quality_ok": True, "shock_veto_ok": True, "trade_geometry_ok": True,
        "risk_ok": True, "rr_ok": True, "volatility_ok": True, "confirmation_family_diversity_ok": True,
        "technical_candidate": bool(btc_ok), "signal_blocked": not btc_ok, "btc_filter_ok": bool(btc_ok),
        "btc_filter_reason": btc_reason, "regime_1d": "BULLISH" if bull else "BEARISH",
        "structure_12h": "CONTEXT_ONLY", "structure_4h": "EMA_TREND",
        "trend_4h": "BULLISH" if bull else "BEARISH", "trigger_type": "THREE_BAR_BREAKOUT",
        "trigger_reason": f"1H close crossed the prior three-bar {('high' if side == 'LONG' else 'low')}",
        "swept_level_1h": level, "target_timeframe": target_tf, "target_levels": [target],
        "stop_source": "Previous five 1H candle extremes + volatility buffer", "atr": atr1, "atr_1h": atr1, "atr_4h": atr4,
        "sl_atr": risk / atr4, "sl_atr_4h": risk / atr4, "sl_atr_1h": risk / atr1,
        "tp_distance_atr": gross_reward / atr4, "tp_distance_atr_1h": gross_reward / atr1,
        "stop_distance_pct": risk / entry, "tp_distance_pct": gross_reward / entry,
        "data_fresh": None, "futures_ok": None, "futures_execution_ok": None,
        "futures_context": {"status": "NOT_CHECKED", "execution_ok": None},
        "closed_1d_candles": len(c1d), "closed_12h_candles": len(c12), "closed_4h_candles": len(c4), "closed_1h_candles": len(c1),
        "candle_open_time": int(current["time"]), "candle_close_time": int(current["time"]) + HOUR_MS,
        "candle_time": int(current["time"]) + HOUR_MS, "entry_time": int(current["time"]) + HOUR_MS,
        "intraday_max_hold_minutes": 72 * 60, "rejection_stage": None, "primary_rejection_reason": None,
        "diagnostic_failures": [], "technical_gate_failures": [], "side_diagnostics": [
            {"side": "LONG", "candidate": side == "LONG", "primary_failure": None if side == "LONG" else "Control bias is SHORT"},
            {"side": "SHORT", "candidate": side == "SHORT", "primary_failure": None if side == "SHORT" else "Control bias is LONG"},
        ], "btc_context": btc_context or {"ok": False, "reason": "not supplied"},
        "reasons": ["Simple trend-control signal"],
    }
