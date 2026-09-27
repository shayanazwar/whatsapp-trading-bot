from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Sequence

from .simulator import SimulatedTrade


@dataclass(frozen=True)
class BacktestSummary:
    days: int
    coins_tested: int
    coins_selected: int
    data_errors: int
    signals: int
    long_signals: int
    short_signals: int
    tp1_hits: int
    tp2_hits: int
    sl_hits: int
    unresolved: int
    resolved: int
    win_rate: float | None
    avg_rr: float | None
    total_r: float | None
    max_losing_streak: int


def summarize(
    *,
    days: int,
    coins_selected: int,
    coins_tested: int,
    data_errors: int,
    trades: Sequence[SimulatedTrade],
) -> BacktestSummary:
    resolved = [trade for trade in trades if trade.outcome in {"TP2", "SL"}]
    winning = [trade for trade in resolved if trade.outcome == "TP2"]
    planned_rr = [trade.planned_rr for trade in trades if trade.planned_rr > 0]
    total_r_values = [trade.r_multiple for trade in resolved if trade.r_multiple is not None]

    max_streak = 0
    current_streak = 0
    for trade in sorted(resolved, key=lambda item: item.signal_time_ms):
        if trade.outcome == "SL":
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return BacktestSummary(
        days=days,
        coins_tested=coins_tested,
        coins_selected=coins_selected,
        data_errors=data_errors,
        signals=len(trades),
        long_signals=sum(1 for trade in trades if trade.side == "LONG"),
        short_signals=sum(1 for trade in trades if trade.side == "SHORT"),
        tp1_hits=sum(1 for trade in trades if trade.tp1_hit),
        tp2_hits=sum(1 for trade in trades if trade.tp2_hit),
        sl_hits=sum(1 for trade in trades if trade.sl_hit),
        unresolved=sum(1 for trade in trades if trade.outcome == "OPEN"),
        resolved=len(resolved),
        win_rate=(len(winning) / len(resolved) * 100.0) if resolved else None,
        avg_rr=mean(planned_rr) if planned_rr else None,
        total_r=sum(total_r_values) if total_r_values else None,
        max_losing_streak=max_streak,
    )


def format_report(summary: BacktestSummary) -> str:
    def number(value: float | None, digits: int = 2) -> str:
        return "N/A" if value is None else f"{value:.{digits}f}"

    def signed(value: float | None, digits: int = 2) -> str:
        return "N/A" if value is None else f"{value:+.{digits}f}R"

    lines = [
        "📊 MEXC ENGINE BACKTEST",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Period: {summary.days} Days",
        "",
        f"🪙 COINS TESTED: {summary.coins_tested}",
        f"📡 SIGNALS: {summary.signals}",
        "",
        f"🟢 LONG: {summary.long_signals}",
        f"🔴 SHORT: {summary.short_signals}",
        "",
        f"🎯 TP1 HIT: {summary.tp1_hits}",
        f"🏆 TP2 HIT: {summary.tp2_hits}",
        f"🛑 SL HIT: {summary.sl_hits}",
        "",
        f"📈 WIN RATE: {number(summary.win_rate, 1)}%",
        f"⚖️ AVG PLANNED RR: {number(summary.avg_rr, 2)}",
        f"💰 TOTAL R: {signed(summary.total_r, 2)}",
        f"📉 MAX LOSING STREAK: {summary.max_losing_streak}",
    ]

    if summary.unresolved:
        lines.append(f"⏳ OPEN/UNRESOLVED: {summary.unresolved}")
    if summary.data_errors:
        lines.append(f"⚠️ DATA ERRORS: {summary.data_errors}")

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "⚠️ PAPER BACKTEST",
            "Win rate = TP2 before SL among resolved trades.",
            "TP1 is a milestone, not a full win.",
            "Current eligible MEXC universe; not a historical-universe test.",
            "Technical engine + historical BTC filter.",
            "Live spread/orderbook/funding/freshness filters are not simulated.",
            "No real trades executed.",
        ]
    )
    return "\n".join(lines)
