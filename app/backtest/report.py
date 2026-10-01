from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, median
from typing import Mapping, Sequence, Any

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
    breakeven_hits: int
    sl_hits: int
    unresolved: int
    resolved: int
    expiry_count: int
    win_rate: float | None
    avg_rr: float | None
    total_r: float | None
    expectancy_r: float | None
    profit_factor: float | None
    max_losing_streak: int
    max_drawdown_r: float | None
    avg_win_r: float | None
    avg_loss_r: float | None
    avg_hold_minutes: float | None
    median_hold_minutes: float | None
    long_win_rate: float | None
    short_win_rate: float | None
    long_expectancy_r: float | None
    short_expectancy_r: float | None
    avg_stop_pct: float | None
    avg_tp2_pct: float | None
    direction_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    regime_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    diagnostics: Mapping[str, int] = field(default_factory=dict)
    quality_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def _resolved(trades: Sequence[SimulatedTrade]) -> list[SimulatedTrade]:
    # EXPIRED trades are finalized at the last available completed price by the
    # stateful simulator, so they are resolved when realized R is available.
    return [
        t for t in trades
        if t.outcome in {"TP2", "SL", "BE", "EXPIRED"}
        and t.r_multiple is not None
    ]


def _win_rate(trades: Sequence[SimulatedTrade]) -> float | None:
    values = _resolved(trades)
    if not values:
        return None
    return 100.0 * sum(t.outcome == "TP2" for t in values) / len(values)


def _expectancy(trades: Sequence[SimulatedTrade]) -> float | None:
    values = [float(t.r_multiple) for t in _resolved(trades)]
    return mean(values) if values else None


def _group_stats(trades: Sequence[SimulatedTrade]) -> dict[str, Mapping[str, Any]]:
    groups: dict[str, list[SimulatedTrade]] = {}
    for trade in trades:
        key = trade.side.upper() if trade.side.upper() in {"LONG", "SHORT"} else "UNKNOWN"
        groups.setdefault(key, []).append(trade)
    out: dict[str, Mapping[str, Any]] = {}
    for key, values in groups.items():
        resolved = _resolved(values)
        out[key] = {
            "signals": len(values),
            "resolved": len(resolved),
            "win_rate": _win_rate(values),
            "expectancy_r": _expectancy(values),
            "total_r": sum(float(t.r_multiple) for t in resolved),
        }
    return out


def _regime_group_stats(trades: Sequence[SimulatedTrade]) -> dict[str, Mapping[str, Any]]:
    groups: dict[str, list[SimulatedTrade]] = {}
    for trade in trades:
        key = str(trade.regime or "UNKNOWN").upper()
        groups.setdefault(key, []).append(trade)
    out: dict[str, Mapping[str, Any]] = {}
    for key, values in groups.items():
        resolved = _resolved(values)
        out[key] = {
            "signals": len(values),
            "resolved": len(resolved),
            "win_rate": _win_rate(values),
            "expectancy_r": _expectancy(values),
            "total_r": sum(float(t.r_multiple) for t in resolved),
        }
    return out


def _quality_stats(trades: Sequence[SimulatedTrade]) -> dict[str, Mapping[str, Any]]:
    resolved = _resolved(trades)
    definitions = (
        ("score", (("82-89", 82.0, 90.0), ("90-100", 90.0, 100.000001))),
        ("bos_15m_strength", (("0.70-0.79", 0.70, 0.80), ("0.80-1.00", 0.80, 1.000001))),
        ("retest_quality", (("0.80-0.89", 0.80, 0.90), ("0.90-1.00", 0.90, 1.000001))),
        ("trigger_quality_5m", (("0.65-0.79", 0.65, 0.80), ("0.80-1.00", 0.80, 1.000001))),
        ("rvol_15m", (("1.10-1.49", 1.10, 1.50), ("1.50+", 1.50, float("inf")))),
        ("adx_4h", (("20-24.9", 20.0, 25.0), ("25+", 25.0, float("inf")))),
    )
    out: dict[str, Mapping[str, Any]] = {}
    for feature, bands in definitions:
        for label, low, high in bands:
            values = [
                t for t in resolved
                if low <= float(t.quality.get(feature, -float("inf"))) < high
            ]
            wins = sum(t.outcome == "TP2" for t in values)
            total = len(values)
            out[f"{feature}:{label}"] = {
                "signals": total,
                "wins": wins,
                "losses": sum(t.outcome == "SL" for t in values),
                "win_rate": (100.0 * wins / total) if total else None,
                "expectancy_r": _expectancy(values),
            }
    return out


def summarize(*, days: int, coins_selected: int, coins_tested: int, data_errors: int, trades: Sequence[SimulatedTrade], diagnostics: Mapping[str, int] | None = None) -> BacktestSummary:
    ordered = sorted(trades, key=lambda t: t.signal_time_ms)
    resolved = _resolved(ordered)
    values = [float(t.r_multiple) for t in resolved]
    winners = [float(t.r_multiple) for t in resolved if t.outcome == "TP2"]
    losers = [float(t.r_multiple) for t in resolved if t.outcome == "SL"]

    running = peak = 0.0
    max_drawdown = 0.0
    losing_streak = max_losing_streak = 0
    for value, trade in zip(values, resolved):
        running += value
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
        if trade.outcome == "SL":
            losing_streak += 1
            max_losing_streak = max(max_losing_streak, losing_streak)
        else:
            losing_streak = 0

    positive = sum(value for value in values if value > 0)
    negative = abs(sum(value for value in values if value < 0))
    profit_factor = positive / negative if negative > 0 else (float("inf") if positive > 0 else None)
    holds = [float(t.hold_minutes) for t in ordered if t.hold_minutes is not None]
    stop_pcts = [abs(t.entry - t.stop_loss) / t.entry * 100.0 for t in ordered if t.entry > 0]
    tp2_pcts = [abs(t.tp2 - t.entry) / t.entry * 100.0 for t in ordered if t.entry > 0]
    planned_rr = [float(t.planned_rr) for t in ordered if t.planned_rr > 0]

    direction_stats = _group_stats(ordered)
    regime_stats = _regime_group_stats(ordered)
    long = direction_stats.get("LONG", {})
    short = direction_stats.get("SHORT", {})

    return BacktestSummary(
        days=days,
        coins_tested=coins_tested,
        coins_selected=coins_selected,
        data_errors=data_errors,
        signals=len(ordered),
        long_signals=sum(t.side.upper() == "LONG" for t in ordered),
        short_signals=sum(t.side.upper() == "SHORT" for t in ordered),
        tp1_hits=sum(t.tp1_hit for t in ordered),
        tp2_hits=sum(t.tp2_hit for t in ordered),
        breakeven_hits=sum(t.breakeven_hit for t in ordered),
        sl_hits=sum(t.sl_hit for t in ordered),
        unresolved=sum(t.r_multiple is None for t in ordered),
        resolved=len(resolved),
        expiry_count=sum(t.expired for t in ordered),
        win_rate=_win_rate(ordered),
        avg_rr=mean(planned_rr) if planned_rr else None,
        total_r=sum(values) if values else 0.0,
        expectancy_r=mean(values) if values else None,
        profit_factor=profit_factor,
        max_losing_streak=max_losing_streak,
        max_drawdown_r=max_drawdown if values else 0.0,
        avg_win_r=mean(winners) if winners else None,
        avg_loss_r=mean(losers) if losers else None,
        avg_hold_minutes=mean(holds) if holds else None,
        median_hold_minutes=median(holds) if holds else None,
        long_win_rate=long.get("win_rate"),
        short_win_rate=short.get("win_rate"),
        long_expectancy_r=long.get("expectancy_r"),
        short_expectancy_r=short.get("expectancy_r"),
        avg_stop_pct=mean(stop_pcts) if stop_pcts else None,
        avg_tp2_pct=mean(tp2_pcts) if tp2_pcts else None,
        direction_stats=direction_stats,
        regime_stats=regime_stats,
        diagnostics=dict(diagnostics or {}),
        quality_stats=_quality_stats(ordered),
    )


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return "N/A" if value is None else f"{value:.{digits}f}{suffix}"


def format_report(summary: BacktestSummary) -> str:
    pf = "∞" if summary.profit_factor == float("inf") else _fmt(summary.profit_factor)
    lines = [
        "📊 MEXC INTRADAY ENGINE BACKTEST",
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
        f"🟡 TP1→BE: {summary.breakeven_hits}",
        f"🛑 SL HIT: {summary.sl_hits}",
        f"⏳ EXPIRED: {summary.expiry_count}",
        "",
        f"📈 WIN RATE: {_fmt(summary.win_rate, 1, '%')}",
        f"⚖️ AVG PLANNED RR: {_fmt(summary.avg_rr)}",
        f"💰 TOTAL R: {"N/A" if summary.total_r is None else f"{summary.total_r:+.2f}R"}",
        f"📊 EXPECTANCY: {"N/A" if summary.expectancy_r is None else f"{summary.expectancy_r:+.2f}R/trade"}",
        f"📐 PROFIT FACTOR: {pf}",
        f"📉 MAX DRAWDOWN: {_fmt(summary.max_drawdown_r)}R",
        f"📉 MAX LOSING STREAK: {summary.max_losing_streak}",
        f"⏱ AVG HOLD: {_fmt(summary.avg_hold_minutes, 1)} min",
        f"⏱ MEDIAN HOLD: {_fmt(summary.median_hold_minutes, 1)} min",
        f"🛑 AVG STOP: {_fmt(summary.avg_stop_pct)}%",
        f"🎯 AVG TP2 DIST: {_fmt(summary.avg_tp2_pct)}%",
        "",
        f"🟢 LONG WIN/EXP: {_fmt(summary.long_win_rate, 1, '%')} / {_fmt(summary.long_expectancy_r)}R",
        f"🔴 SHORT WIN/EXP: {_fmt(summary.short_win_rate, 1, '%')} / {_fmt(summary.short_expectancy_r)}R",
        f"⏳ OPEN/UNRESOLVED: {summary.unresolved}",
        f"⚠️ DATA ERRORS: {summary.data_errors}",
    ]
    if summary.regime_stats:
        lines.append("")
        lines.append("REGIME STATS")
        for key in sorted(summary.regime_stats):
            stat = summary.regime_stats[key]
            lines.append(f"{key}: n={stat['signals']} WR={_fmt(stat['win_rate'], 1, '%')} Exp={_fmt(stat['expectancy_r'])}R")
    useful_quality = [
        (key, stat) for key, stat in summary.quality_stats.items()
        if int(stat.get("signals", 0) or 0) > 0
    ]
    if useful_quality:
        lines.append("")
        lines.append("FORENSIC QUALITY BUCKETS")
        for key, stat in useful_quality[:8]:
            lines.append(
                f"{key}: n={stat['signals']} WR={_fmt(stat['win_rate'], 1, '%')} "
                f"Exp={_fmt(stat['expectancy_r'])}R"
            )
    if summary.diagnostics:
        d = summary.diagnostics
        gate_keys = (
            "CANDIDATES_DISCOVERED",
            "CANDIDATES_ENGINE_EVALUATED",
            "ENGINE_SUCCESS",
            "ENGINE_ERRORS",
            "TECHNICAL_ACCEPT",
            "TECHNICAL_REJECT",
            "BTC_REJECT",
            "SIMULATION_ACCEPT",
            "SIMULATION_NO_TRADE",
        )
        gate_lines = [(key, int(d.get(key, 0) or 0)) for key in gate_keys if key in d]
        reject_keys = sorted(
            (
                (key.replace("ENGINE_REJECT_", ""), int(value))
                for key, value in d.items()
                if key.startswith("ENGINE_REJECT_") and int(value or 0) > 0
            ),
            key=lambda item: (-item[1], item[0]),
        )
        if gate_lines or reject_keys:
            lines.append("")
            lines.append("GATE FUNNEL")
            lines.extend(f"{key}: {value}" for key, value in gate_lines)
            if reject_keys:
                lines.append("TOP ENGINE REJECTIONS")
                lines.extend(f"{key}: {value}" for key, value in reject_keys[:12])

        top = sorted(summary.diagnostics.items(), key=lambda item: (-item[1], item[0]))[:8]
        if top:
            lines.append("")
            lines.append("FORENSIC COUNTERS")
            lines.extend(f"{key}: {value}" for key, value in top)
    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        "⚠️ PAPER BACKTEST",
        "15M defines the intraday setup; 5M is the immediate execution trigger.",
        "Win rate = TP2 before SL among resolved trades.",
        "TP1 is a milestone, not a full win.",
        "Costs/slippage are included in resolved R.",
        "Current eligible MEXC universe; not a historical-universe test.",
        "Historical BTC filter is included; live orderbook/freshness filters are not.",
        "No real trades executed.",
    ])
    return "\n".join(lines)
