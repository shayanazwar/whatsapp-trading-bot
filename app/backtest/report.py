from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean, median
from typing import Any, Mapping, Sequence

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
    tp_hits: int
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
    avg_tp_pct: float | None
    direction_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    regime_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    diagnostics: Mapping[str, int] = field(default_factory=dict)
    quality_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def _resolved(trades: Sequence[SimulatedTrade]) -> list[SimulatedTrade]:
    return [
        trade
        for trade in trades
        if trade.outcome in {"TP", "SL", "EXPIRED"}
        and trade.r_multiple is not None
        and math.isfinite(float(trade.r_multiple))
    ]


def _win_rate(trades: Sequence[SimulatedTrade]) -> float | None:
    values = _resolved(trades)
    if not values:
        return None
    return 100.0 * sum(trade.outcome == "TP" for trade in values) / len(values)


def _expectancy(trades: Sequence[SimulatedTrade]) -> float | None:
    values = [float(trade.r_multiple) for trade in _resolved(trades)]
    return mean(values) if values else None


def _group_stats(trades: Sequence[SimulatedTrade]) -> dict[str, Mapping[str, Any]]:
    groups: dict[str, list[SimulatedTrade]] = {}
    for trade in trades:
        side = str(trade.side).upper()
        key = side if side in {"LONG", "SHORT"} else "UNKNOWN"
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
        ("score", (("78-89", 78.0, 90.0), ("90-100", 90.0, 100.000001))),
        ("bos_15m_strength", (("0.70-0.79", 0.70, 0.80), ("0.80-1.00", 0.80, 1.000001))),
        ("retest_quality", (("0.80-0.89", 0.80, 0.90), ("0.90-1.00", 0.90, 1.000001))),
        ("rvol_15m", (("1.00-1.49", 1.00, 1.50), ("1.50+", 1.50, float("inf")))),
        ("adx_4h", (("20-24.9", 20.0, 25.0), ("25+", 25.0, float("inf")))),
    )

    out: dict[str, Mapping[str, Any]] = {}
    for feature, bands in definitions:
        for label, low, high in bands:
            values = [
                trade
                for trade in resolved
                if low <= float(trade.quality.get(feature, -float("inf"))) < high
            ]
            wins = sum(trade.outcome == "TP" for trade in values)
            total = len(values)
            out[f"{feature}:{label}"] = {
                "signals": total,
                "wins": wins,
                "losses": sum(trade.outcome == "SL" for trade in values),
                "win_rate": (100.0 * wins / total) if total else None,
                "expectancy_r": _expectancy(values),
            }
    return out


def summarize(
    *,
    days: int,
    coins_selected: int,
    coins_tested: int,
    data_errors: int,
    trades: Sequence[SimulatedTrade],
    diagnostics: Mapping[str, int] | None = None,
) -> BacktestSummary:
    ordered = sorted(trades, key=lambda trade: trade.signal_time_ms)
    resolved = _resolved(ordered)
    values = [float(trade.r_multiple) for trade in resolved]
    winners = [float(trade.r_multiple) for trade in resolved if trade.outcome == "TP"]
    losers = [float(trade.r_multiple) for trade in resolved if trade.outcome == "SL"]

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
    holds = [float(trade.hold_minutes) for trade in ordered if trade.hold_minutes is not None]
    stop_pcts = [
        abs(float(trade.entry) - float(trade.stop_loss)) / float(trade.entry) * 100.0
        for trade in ordered
        if float(trade.entry) > 0
    ]
    tp_pcts = [
        abs(float(trade.tp) - float(trade.entry)) / float(trade.entry) * 100.0
        for trade in ordered
        if float(trade.entry) > 0
    ]
    planned_rr = [float(trade.planned_rr) for trade in ordered if float(trade.planned_rr) > 0]

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
        long_signals=sum(str(trade.side).upper() == "LONG" for trade in ordered),
        short_signals=sum(str(trade.side).upper() == "SHORT" for trade in ordered),
        tp_hits=sum(bool(trade.tp1_hit) for trade in ordered),
        sl_hits=sum(bool(trade.sl_hit) for trade in ordered),
        unresolved=sum(trade.r_multiple is None for trade in ordered),
        resolved=len(resolved),
        expiry_count=sum(bool(trade.expired) for trade in ordered),
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
        avg_tp_pct=mean(tp_pcts) if tp_pcts else None,
        direction_stats=direction_stats,
        regime_stats=regime_stats,
        diagnostics=dict(diagnostics or {}),
        quality_stats=_quality_stats(ordered),
    )


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return "N/A" if value is None else f"{value:.{digits}f}{suffix}"


def format_report(summary: BacktestSummary) -> str:
    pf = "∞" if summary.profit_factor == float("inf") else _fmt(summary.profit_factor)
    total_r = "N/A" if summary.total_r is None else f"{summary.total_r:+.2f}R"
    expectancy = "N/A" if summary.expectancy_r is None else f"{summary.expectancy_r:+.2f}R/trade"

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
        f"🎯 TP HIT: {summary.tp_hits}",
        f"🛑 SL HIT: {summary.sl_hits}",
        f"⏳ EXPIRED: {summary.expiry_count}",
        "",
        f"📈 WIN RATE: {_fmt(summary.win_rate, 1, '%')}",
        f"⚖️ AVG PLANNED RR: {_fmt(summary.avg_rr)}",
        f"💰 TOTAL R: {total_r}",
        f"📊 EXPECTANCY: {expectancy}",
        f"📐 PROFIT FACTOR: {pf}",
        f"📉 MAX DRAWDOWN: {_fmt(summary.max_drawdown_r)}R",
        f"📉 MAX LOSING STREAK: {summary.max_losing_streak}",
        f"⏱ AVG HOLD: {_fmt(summary.avg_hold_minutes, 1)} min",
        f"⏱ MEDIAN HOLD: {_fmt(summary.median_hold_minutes, 1)} min",
        f"🛑 AVG STOP: {_fmt(summary.avg_stop_pct)}%",
        f"🎯 AVG TP DIST: {_fmt(summary.avg_tp_pct)}%",
        "",
        f"🟢 LONG WIN/EXP: {_fmt(summary.long_win_rate, 1, '%')} / {_fmt(summary.long_expectancy_r)}R",
        f"🔴 SHORT WIN/EXP: {_fmt(summary.short_win_rate, 1, '%')} / {_fmt(summary.short_expectancy_r)}R",
        f"⏳ OPEN/UNRESOLVED: {summary.unresolved}",
        f"⚠️ DATA ERRORS: {summary.data_errors}",
    ]

    if summary.regime_stats:
        lines.extend(("", "REGIME STATS"))
        for key in sorted(summary.regime_stats):
            stat = summary.regime_stats[key]
            lines.append(
                f"{key}: n={stat['signals']} WR={_fmt(stat['win_rate'], 1, '%')} "
                f"Exp={_fmt(stat['expectancy_r'])}R"
            )

    useful_quality = [
        (key, stat)
        for key, stat in summary.quality_stats.items()
        if int(stat.get("signals", 0) or 0) > 0
    ]
    if useful_quality:
        lines.extend(("", "FORENSIC QUALITY BUCKETS"))
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
            "STRUCTURE_WINDOW_REVISITS",
            "DUPLICATE_STRUCTURE_SKIPPED",
            "FIVE_MINUTE_CONFIRMATION_BYPASSED",
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
            lines.extend(("", "GATE FUNNEL"))
            lines.extend(f"{key}: {value}" for key, value in gate_lines)
            if reject_keys:
                lines.append("TOP ENGINE REJECTIONS")
                lines.extend(f"{key}: {value}" for key, value in reject_keys[:12])

        top = sorted(summary.diagnostics.items(), key=lambda item: (-int(item[1]), item[0]))[:8]
        if top:
            lines.extend(("", "FORENSIC COUNTERS"))
            lines.extend(f"{key}: {value}" for key, value in top)

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "⚠️ PAPER BACKTEST",
            "15M BOS + retest defines the setup; 5M is not a signal-confirmation gate.",
            "Win rate = single TP before SL among resolved trades.",
            "ONE TP and ONE SL; no partial close or breakeven stage.",
            "Costs/slippage are included in realized R.",
            "Current eligible MEXC universe; not a historical-universe test.",
            "Historical BTC filter is included; live orderbook/freshness filters are not.",
            "No real trades executed.",
        ]
    )
    return "\n".join(lines)
