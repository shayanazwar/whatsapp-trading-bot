from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Mapping, Sequence

from .simulator import SimulatedTrade


@dataclass(frozen=True)
class BacktestSummary:
    days: int
    period_start_ms: int
    period_end_ms: int
    timeframes: tuple[str, ...]
    coins_tested: int
    coins_selected: int
    data_errors: int
    execution_errors: int
    rejected_setups: int
    signals: int
    long_signals: int
    short_signals: int
    resolved: int
    unresolved: int
    tp_hits: int
    sl_hits: int
    expired: int
    win_rate: float | None
    avg_planned_rr: float | None
    avg_realized_r: float | None
    total_r: float
    expectancy_r: float | None
    profit_factor: float | None
    max_drawdown_r: float
    max_losing_streak: int
    diagnostics: Mapping[str, int] = field(default_factory=dict)


def _resolved(trades: Sequence[SimulatedTrade]) -> list[SimulatedTrade]:
    return [t for t in trades if t.outcome in {"TP", "SL", "EXPIRED"} and t.r_multiple is not None and math.isfinite(float(t.r_multiple))]


def summarize(*, days: int, period_start_ms: int, period_end_ms: int, coins_selected: int, coins_tested: int, data_errors: int, execution_errors: int, rejected_setups: int, trades: Sequence[SimulatedTrade], diagnostics: Mapping[str, int] | None = None) -> BacktestSummary:
    ordered = sorted(trades, key=lambda t: (int(t.signal_time_ms), t.symbol, t.side))
    resolved = _resolved(ordered)
    values = [float(t.r_multiple) for t in resolved]
    wins = sum(t.outcome == "TP" for t in resolved)
    losses = sum(t.outcome == "SL" for t in resolved)
    expired = sum(t.outcome == "EXPIRED" for t in resolved)
    running = peak = 0.0
    drawdown = 0.0
    losing_streak = max_streak = 0
    for t in resolved:
        value = float(t.r_multiple)
        running += value
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
        if t.outcome == "SL":
            losing_streak += 1
            max_streak = max(max_streak, losing_streak)
        else:
            losing_streak = 0
    positive = sum(v for v in values if v > 0)
    negative = abs(sum(v for v in values if v < 0))
    pf = positive / negative if negative > 0 else (float("inf") if positive > 0 else None)
    planned = [abs(float(t.tp1) - float(t.entry)) / abs(float(t.entry) - float(t.stop_loss)) for t in ordered if abs(float(t.entry) - float(t.stop_loss)) > 0]
    diagnostics_out = dict(diagnostics or {})
    diagnostics_out.setdefault("SIGNALS", len(ordered))
    diagnostics_out.setdefault("RESOLVED", len(resolved))
    diagnostics_out.setdefault("TP_HIT", wins)
    diagnostics_out.setdefault("SL_HIT", losses)
    diagnostics_out.setdefault("EXPIRED", expired)
    return BacktestSummary(
        days=days,
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        timeframes=("1D", "12H", "4H", "1H"),
        coins_tested=coins_tested,
        coins_selected=coins_selected,
        data_errors=data_errors,
        execution_errors=execution_errors,
        rejected_setups=rejected_setups,
        signals=len(ordered),
        long_signals=sum(str(t.side).upper() == "LONG" for t in ordered),
        short_signals=sum(str(t.side).upper() == "SHORT" for t in ordered),
        resolved=len(resolved),
        unresolved=sum(t.r_multiple is None for t in ordered),
        tp_hits=wins,
        sl_hits=losses,
        expired=expired,
        win_rate=(100.0 * wins / (wins + losses)) if wins + losses else None,
        avg_planned_rr=mean(planned) if planned else None,
        avg_realized_r=mean(values) if values else None,
        total_r=sum(values),
        expectancy_r=mean(values) if values else None,
        profit_factor=pf,
        max_drawdown_r=drawdown,
        max_losing_streak=max_streak,
        diagnostics=diagnostics_out,
    )


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return "N/A" if value is None else f"{value:.{digits}f}{suffix}"


def format_report(summary: BacktestSummary) -> str:
    start = summary.period_start_ms // 1000
    end = summary.period_end_ms // 1000
    pf = "∞" if summary.profit_factor == float("inf") else _fmt(summary.profit_factor)
    lines = [
        "📊 MEXC SWING ENGINE BACKTEST",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Period: {summary.days}D ({start} → {end})",
        f"Timeframes: {' / '.join(summary.timeframes)}",
        "",
        f"🪙 Coins Tested: {summary.coins_tested}",
        f"📡 Signals: {summary.signals}",
        f"🟢 LONG: {summary.long_signals}",
        f"🔴 SHORT: {summary.short_signals}",
        f"✅ Resolved: {summary.resolved}",
        f"⏳ Open/Unresolved: {summary.unresolved}",
        f"🎯 TP: {summary.tp_hits}",
        f"🛑 SL: {summary.sl_hits}",
        f"⌛ Expired: {summary.expired}",
        "",
        f"📈 Win Rate: {_fmt(summary.win_rate, 1, '%')}",
        f"⚖️ Avg Planned RR: {_fmt(summary.avg_planned_rr)}",
        f"💰 Avg Realized R: {_fmt(summary.avg_realized_r)}R",
        f"💰 Total R: {summary.total_r:+.2f}R",
        f"📊 Expectancy: {_fmt(summary.expectancy_r)}R/trade",
        f"📐 Profit Factor: {pf}",
        f"📉 Max Drawdown: {summary.max_drawdown_r:.2f}R",
        f"📉 Max Losing Streak: {summary.max_losing_streak}",
        "",
        f"⚠️ Data Errors: {summary.data_errors}",
        f"⚠️ Execution Errors: {summary.execution_errors}",
        f"🚫 Rejected Setups: {summary.rejected_setups}",
    ]
    if summary.diagnostics:
        lines.extend(("", "GATE DIAGNOSTICS"))
        important = sorted(summary.diagnostics.items(), key=lambda item: (-int(item[1]), item[0]))
        lines.extend(f"{key}: {value}" for key, value in important[:20])
    lines.extend(("━━━━━━━━━━━━━━━━━━━━", "⚠️ PAPER BACKTEST", "Decisions use only completed 1D / 12H / 4H / 1H candles.", "12H is a causal aggregation of three contiguous completed 4H candles.", "No real trades executed. Fees/slippage are simulated conservatively."))
    return "\n".join(lines)
