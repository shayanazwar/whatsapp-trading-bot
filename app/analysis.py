"""Deterministic multi-timeframe market analysis and WhatsApp-friendly report formatting."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.market.candles import Candle, STRATEGY_TIMEFRAMES
from app.market.indicators import atr, ema, rsi
from app.strategy.rfpc import evaluate_strategy


def analyze_series(symbol: str, series: Mapping[str, Sequence[Candle]], *, decision_time: int | None = None,
                   data_warnings: Sequence[str] = ()) -> dict[str, Any]:
    if set(series) - STRATEGY_TIMEFRAMES:
        raise ValueError("Analysis accepts only 1D, 12H, 4H and 1H strategy candles")
    if decision_time is None:
        completed_h1 = [c.close_time for c in series.get("1H", ()) if c.complete]
        decision_time = max(completed_h1, default=0)
    evaluation = evaluate_strategy(symbol, series, decision_time=decision_time)
    metrics: dict[str, Any] = {}
    for tf in ("1D", "12H", "4H", "1H"):
        bars = [c for c in series.get(tf, ()) if c.complete and c.close_time <= decision_time]
        if not bars:
            metrics[tf] = {"bars": 0, "status": "NO_COMPLETED_DATA"}
            continue
        closes = [c.close for c in bars]
        highs = [c.high for c in bars]
        lows = [c.low for c in bars]
        ema21 = ema(closes, 21)[-1]
        ema50 = ema(closes, 50)[-1]
        ema100 = ema(closes, 100)[-1]
        ema200 = ema(closes, 200)[-1]
        rsi14 = rsi(closes, 14)[-1]
        atr14 = atr(highs, lows, closes, 14)[-1]
        metrics[tf] = {
            "bars": len(bars), "last_close": closes[-1], "last_close_time": bars[-1].close_time,
            "ema21": ema21, "ema50": ema50, "ema100": ema100, "ema200": ema200,
            "rsi14": rsi14, "atr14": atr14,
            "ema_stack": (
                "EMA21>EMA50" if ema21 is not None and ema50 is not None and ema21 > ema50
                else "EMA21<EMA50" if ema21 is not None and ema50 is not None and ema21 < ema50
                else "EMA_STACK_UNAVAILABLE"
            ),
        }
    warnings = list(data_warnings)
    if any(metrics[tf].get("bars", 0) == 0 for tf in STRATEGY_TIMEFRAMES):
        warnings.append("At least one required strategy timeframe lacks completed data.")
    if len(series.get("1D", ())) < 200:
        warnings.append("The 1D EMA200 regime is unavailable until sufficient daily history is collected.")
    return {
        "symbol": symbol,
        "decision_time": decision_time,
        "regime": evaluation.regime,
        "signal": "CANDIDATE" if evaluation.candidate else "NO_VALID_SETUP",
        "candidate": evaluation.candidate.as_dict() if evaluation.candidate else None,
        "rejection_reasons": list(evaluation.rejection_reasons),
        "timeframes": metrics,
        "warnings": list(dict.fromkeys(warnings)),
        "strategy_version": "S1-RFPC-0.2.0-1H-trigger",
        "disclaimer": "Research hypothesis only; no strategy profitability has been established.",
    }


def format_analysis(report: Mapping[str, Any]) -> str:
    symbol = report.get("symbol", "UNKNOWN")
    regime = report.get("regime", "UNKNOWN")
    signal = report.get("signal", "UNKNOWN")
    lines = [f"ANALYSIS — {symbol}", f"Regime: {regime}", f"Decision: {signal}"]
    frames = report.get("timeframes", {})
    for tf in ("1D", "12H", "4H", "1H"):
        row = frames.get(tf, {})
        if not row or row.get("bars", 0) == 0:
            lines.append(f"{tf}: no completed data")
            continue
        def fmt(value: Any) -> str:
            return "n/a" if value is None else f"{float(value):.6g}"
        lines.append(
            f"{tf}: close {fmt(row.get('last_close'))} | EMA21 {fmt(row.get('ema21'))} | "
            f"EMA50 {fmt(row.get('ema50'))} | RSI14 {fmt(row.get('rsi14'))} | ATR14 {fmt(row.get('atr14'))}"
        )
    candidate = report.get("candidate")
    if isinstance(candidate, dict):
        lines.extend([
            f"Side: {candidate.get('side')}",
            f"Entry reference (not a fill): {candidate.get('entry_reference')}",
            f"Stop: {candidate.get('stop_price')} | Target: {candidate.get('target_price')}",
            f"Reason: {candidate.get('reason')}",
        ])
    else:
        reasons = report.get("rejection_reasons") or []
        lines.append("Why no setup: " + (", ".join(str(x) for x in reasons) if reasons else "data or structure not sufficient"))
    warnings = report.get("warnings") or []
    if warnings:
        lines.append("Warnings: " + "; ".join(str(x) for x in warnings[:4]))
    lines.append("Research hypothesis only; not a trading result or guarantee.")
    return "\n".join(lines)
