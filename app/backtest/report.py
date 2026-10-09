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
    avg_signal_rr: float | None
    avg_actual_fill_rr: float | None
    avg_realized_r: float | None
    total_r: float
    expectancy_r: float | None
    profit_factor: float | None
    max_drawdown_r: float
    max_losing_streak: int
    avg_mae_r: float | None = None
    avg_mfe_r: float | None = None
    market_entries: int = 0
    limit_entries: int = 0
    diagnostics: Mapping[str, int] = field(default_factory=dict)
    engine_version: str = ""
    engine_config_fingerprint: str = ""
    engine_config: Mapping[str, Any] = field(default_factory=dict)


def _resolved(trades: Sequence[SimulatedTrade]) -> list[SimulatedTrade]:
    return [t for t in trades if t.outcome in {"TP", "SL", "EXPIRED"} and t.r_multiple is not None and math.isfinite(float(t.r_multiple))]


def summarize(
    *,
    days: int,
    period_start_ms: int,
    period_end_ms: int,
    coins_selected: int,
    coins_tested: int,
    data_errors: int,
    execution_errors: int,
    rejected_setups: int,
    trades: Sequence[SimulatedTrade],
    diagnostics: Mapping[str, int] | None = None,
    engine_version: str = "",
    engine_config_fingerprint: str = "",
    engine_config: Mapping[str, Any] | None = None,
) -> BacktestSummary:
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
    signal_rr_values = [
        float(getattr(t, "signal_rr", 0.0))
        for t in ordered
        if getattr(t, "signal_rr", None) is not None and math.isfinite(float(getattr(t, "signal_rr")))
    ]
    actual_fill_rr_values = [
        float(t.actual_fill_rr)
        for t in ordered
        if getattr(t, "actual_fill_rr", None) is not None and math.isfinite(float(t.actual_fill_rr))
    ]
    diagnostics_out = dict(diagnostics or {})
    diagnostics_out.setdefault("SIGNALS", len(ordered))
    diagnostics_out.setdefault("RESOLVED", len(resolved))
    diagnostics_out.setdefault("TP_HIT", wins)
    diagnostics_out.setdefault("SL_HIT", losses)
    diagnostics_out.setdefault("EXPIRED", expired)
    diagnostics_out.setdefault("PORTFOLIO_SKIPPED", 0)
    mae_values = [float(t.mae_r) for t in ordered if t.mae_r is not None and math.isfinite(float(t.mae_r))]
    mfe_values = [float(t.mfe_r) for t in ordered if t.mfe_r is not None and math.isfinite(float(t.mfe_r))]
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
        unresolved=sum(str(t.outcome).upper() == "OPEN" for t in ordered),
        tp_hits=wins,
        sl_hits=losses,
        expired=expired,
        win_rate=(100.0 * wins / (wins + losses)) if wins + losses else None,
        avg_planned_rr=mean(signal_rr_values) if signal_rr_values else None,
        avg_signal_rr=mean(signal_rr_values) if signal_rr_values else None,
        avg_actual_fill_rr=mean(actual_fill_rr_values) if actual_fill_rr_values else None,
        avg_realized_r=mean(values) if values else None,
        total_r=sum(values),
        expectancy_r=mean(values) if values else None,
        profit_factor=pf,
        max_drawdown_r=drawdown,
        max_losing_streak=max_streak,
        avg_mae_r=mean(mae_values) if mae_values else None,
        avg_mfe_r=mean(mfe_values) if mfe_values else None,
        market_entries=sum(1 for t in ordered if str(getattr(t, "entry_mode", "MARKET")).upper() != "LIMIT"),
        limit_entries=sum(1 for t in ordered if str(getattr(t, "entry_mode", "MARKET")).upper() == "LIMIT"),
        diagnostics=diagnostics_out,
        engine_version=str(engine_version or ""),
        engine_config_fingerprint=str(engine_config_fingerprint or ""),
        engine_config=dict(engine_config or {}),
    )


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return "N/A" if value is None else f"{value:.{digits}f}{suffix}"


def format_report(summary: BacktestSummary) -> str:
    start = summary.period_start_ms // 1000
    end = summary.period_end_ms // 1000
    pf = "∞" if summary.profit_factor == float("inf") else _fmt(summary.profit_factor)
    tp_mode_x100 = int(summary.diagnostics.get("TP_MODE_X100", 0)) if summary.diagnostics else 0
    tp_mode = "CONTROL" if tp_mode_x100 == 0 else f"{tp_mode_x100 / 100.0:.1f}R"
    lines = [
        "📊 MEXC SWING ENGINE BACKTEST",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Period: {summary.days}D ({start} → {end})",
        f"TP MODE: {tp_mode}",
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
        f"⚖️ Avg Signal RR: {_fmt(summary.avg_signal_rr)}",
        f"🎯 Avg Actual Fill RR: {_fmt(summary.avg_actual_fill_rr)}",
        f"💰 Avg Realized R: {_fmt(summary.avg_realized_r, 2, 'R')}",
        f"💰 Total R: {summary.total_r:+.2f}R",
        f"📊 Expectancy: {_fmt(summary.expectancy_r, 2, 'R/trade')}",
        f"📐 Profit Factor: {pf}",
        f"📉 Max Drawdown: {summary.max_drawdown_r:.2f}R",
        f"📉 Max Losing Streak: {summary.max_losing_streak}",
        f"🧭 Avg MAE / MFE: {_fmt(summary.avg_mae_r, 2, 'R')} / {_fmt(summary.avg_mfe_r, 2, 'R')}",
        f"⚙️ Entry Mode: MARKET {summary.market_entries} / LIMIT {summary.limit_entries}",
        "",
        f"⚠️ Data Errors: {summary.data_errors}",
        f"⚠️ Execution Errors: {summary.execution_errors}",
        f"🚫 Rejected Setups: {summary.rejected_setups}",
    ]
    diagnostics = summary.diagnostics or {}
    fixed_period = int(diagnostics.get("FIXED_BACKTEST_PERIOD_ENABLED", 0)) > 0
    if summary.engine_version:
        lines.insert(5, f"🧬 Engine Build: {summary.engine_version}")
    config = summary.engine_config or {}
    if config:
        impulse = float(config.get("min_impulse_atr", 2.50))
        trigger_bars = int(config.get("max_trigger_bars_1h", 6))
        stop_mode = str(config.get("stop_loss_mode", "UNKNOWN"))
        short_relaxed = "ON" if bool(config.get("short_range_relaxed", False)) else "OFF"
        lines.insert(
            6,
            f"⚙️ Effective Config: Impulse {impulse:.2f} ATR | Reclaim {trigger_bars} x 1H | SL {stop_mode} | Short-range relaxed {short_relaxed}",
        )
    if summary.engine_config_fingerprint:
        lines.insert(7, f"🔐 Config Fingerprint: {summary.engine_config_fingerprint}")
    lines.insert(
        3,
        "🗓️ Window Mode: FIXED" if fixed_period else "🗓️ Window Mode: ROLLING (set V11_BACKTEST_START_MS / V11_BACKTEST_END_MS for comparisons)",
    )
    cf = diagnostics.get("TP_COUNTERFACTUAL_R_X100")
    if cf is not None:
        lines.insert(3, f"🧪 TP Counterfactual: {float(cf) / 100.0:.2f}R (exit-only; entries/SL unchanged)")
        lines.insert(5, "🧭 TP comparison uses CONTROL exit schedule for candidate eligibility")
    if summary.diagnostics:
        active_experiments = []
        sl_origin = int(summary.diagnostics.get("V11_SL_MODE_4H_ORIGIN", 0))
        impulse_x100 = int(summary.diagnostics.get("V11_MIN_IMPULSE_ATR_X100", 250))
        trigger_bars = int(summary.diagnostics.get("V11_MAX_TRIGGER_BARS", 6))
        short_relaxed = int(summary.diagnostics.get("V11_SHORT_RANGE_RELAXED", 0))
        if sl_origin:
            active_experiments.append("SL=4H_ORIGIN")
        if impulse_x100 != 250:
            active_experiments.append(f"4H_IMPULSE={impulse_x100 / 100.0:.2f}ATR")
        if trigger_bars != 6:
            active_experiments.append(f"SWEEP_RECLAIM_WINDOW={trigger_bars}B")
        if short_relaxed:
            active_experiments.append("SHORT_RANGE_RELAXED=ON")
        if active_experiments:
            lines.insert(4, "🧪 V11 Experiment Config: " + " | ".join(active_experiments))
        lines.extend(("", "GATE DIAGNOSTICS"))
        important = sorted(summary.diagnostics.items(), key=lambda item: (-int(item[1]), item[0]))
        lines.extend(f"{key}: {value}" for key, value in important[:20])
    lines.extend(("━━━━━━━━━━━━━━━━━━━━", "⚠️ PAPER BACKTEST", "Decisions use only completed 1D / 12H / 4H / 1H candles.", "12H is a causal aggregation of three contiguous completed 4H candles.", "No real trades executed. Fees/slippage are simulated conservatively."))
    return "\n".join(lines)
