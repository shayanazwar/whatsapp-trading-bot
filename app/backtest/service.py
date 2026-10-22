"""Shared, single-fetch backtest service used by Telegram commands and automation."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4

from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.config import Settings
from app.data.binance_futures import MexcFuturesClient
from app.data.models import Candle
from app.data.validation import validate_requested_coverage
from app.persistence.store import SQLiteStore
from app.strategy.engine import ICTSilverBulletStrategy, StrategyConfig, STRATEGY_VERSION

TP_R_MULTIPLES: tuple[float, ...] = (1.0, 1.2, 1.5, 2.0)
BACKTEST_TIMEFRAME_SECONDS = 300
MAX_BACKTEST_DAYS = 30


def aligned_end_time(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    epoch = int(current.timestamp())
    aligned = epoch - epoch % BACKTEST_TIMEFRAME_SECONDS
    return datetime.fromtimestamp(aligned, timezone.utc)


def _strategy_from_settings(settings: Settings) -> ICTSilverBulletStrategy:
    # Use the same structural rules but a low candidate floor so 1R and 1.2R aren't
    # biased by the live signal's usual 1.5R native-target filter.
    return ICTSilverBulletStrategy(StrategyConfig(
        interval_seconds=BACKTEST_TIMEFRAME_SECONDS,
        pivot_left=settings.strategy_pivot_left,
        pivot_right=settings.strategy_pivot_right,
        atr_period=settings.strategy_atr_period,
        displacement_atr_multiple=settings.strategy_displacement_atr,
        max_bars_after_sweep=settings.strategy_max_bars_after_sweep,
        min_reward_to_risk=0.1,
        stop_buffer_atr=settings.strategy_stop_buffer_atr,
        signal_expiry_minutes=settings.strategy_signal_expiry_minutes,
    ))


def _engine_from_settings(settings: Settings) -> BacktestEngine:
    return BacktestEngine(
        strategy=_strategy_from_settings(settings),
        config=BacktestConfig(
            initial_capital=settings.initial_capital,
            risk_per_trade_pct=settings.risk_per_trade_pct,
            fee_bps_per_side=settings.fee_bps_per_side,
            slippage_bps_per_side=settings.slippage_bps_per_side,
            funding_bps_per_8h=settings.funding_bps_per_8h,
            max_concurrent_positions=settings.max_concurrent_positions,
            max_leverage=1.0,
        ),
    )


async def run_rr_comparison(
    settings: Settings,
    store: SQLiteStore,
    symbols: tuple[str, ...] | list[str],
    days: int = 7,
) -> dict[str, object]:
    """Fetch one historical data set, generate candidates once, test four TP variants, persist summary."""
    normalized_symbols = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    if not normalized_symbols:
        raise ValueError("At least one symbol is required for a backtest")
    if any(not re.fullmatch(r"[A-Z0-9]{2,30}", symbol) for symbol in normalized_symbols):
        raise ValueError("Symbols must contain only 2-30 ASCII letters/numbers")
    if not 1 <= days <= MAX_BACKTEST_DAYS:
        raise ValueError(f"Backtest period must be between 1 and {MAX_BACKTEST_DAYS} days")

    started_at = datetime.now(timezone.utc)
    wall_started = monotonic()
    end = aligned_end_time(started_at)
    start = end - timedelta(days=days)
    # Eight days of pre-roll supplies prior-week liquidity and pivot context. Only signals
    # created inside [start, end) are scored; the warmup period is not counted as test time.
    fetch_start = start - timedelta(days=8)
    data: dict[str, list[Candle]] = {}
    coverage: dict[str, dict[str, object]] = {}
    async with MexcFuturesClient(base_url=settings.mexc_base_url) as client:
        # Sequential bounded fetches respect the exchange's public API rate limit.
        for symbol in normalized_symbols:
            candles = await client.fetch_ohlcv(
                symbol, "5m", fetch_start, end, limit=1000,
            )
            if len(candles) < 50:
                raise RuntimeError(f"Only {len(candles)} candles received for {symbol}; backtest aborted")
            coverage[symbol] = validate_requested_coverage(candles, fetch_start, end, BACKTEST_TIMEFRAME_SECONDS)
            data[symbol] = candles

    engine = _engine_from_settings(settings)
    scenario_results = await asyncio.to_thread(
        engine.run_scenarios, data, TP_R_MULTIPLES, settings.backtest_fixed_risk_usd, start, end,
    )
    run_id = uuid4().hex[:12]
    completed_at = datetime.now(timezone.utc)
    runtime_seconds = round(monotonic() - wall_started, 3)
    scenarios: dict[str, dict[str, object]] = {}
    for label, result in scenario_results.items():
        metrics = result["metrics"]
        # Surface why candidates did not become trades. These counts are the same across
        # TP variants because target R only changes the exit after entry has filled.
        entry_status_counts: dict[str, int] = {}
        entry_status_notes: list[str] = []
        for trade in result.get("trades", []):
            status = str(getattr(trade, "status", "UNKNOWN"))
            entry_status_counts[status] = entry_status_counts.get(status, 0) + 1
            note = str(getattr(trade, "note", "")).strip()
            if status.startswith("UNFILLED_") and note and note not in entry_status_notes:
                entry_status_notes.append(note)
        scenarios[label] = {
            "target_r_multiple": float(label[:-1]),
            "gross_target_reward_usd_before_costs": round(settings.backtest_fixed_risk_usd * float(label[:-1]), 2),
            "metrics": metrics,
            "signals_total": metrics["signals_total"],
            "entered_trades": metrics["entered_trades"],
            "resolved_trades": metrics["resolved_trades"],
            "entry_status_counts": entry_status_counts,
            "entry_status_notes": entry_status_notes[:3],
            "wins": metrics["wins"],
            "losses": metrics["losses"],
            "win_rate_pct_resolved_denominator": metrics["win_rate_pct_resolved_denominator"],
            "net_pnl_resolved": metrics["net_pnl_resolved"],
            "profit_factor_net_resolved": metrics["profit_factor_net_resolved"],
            "expectancy_r_resolved": metrics["expectancy_r_resolved"],
            "max_drawdown_pct_realized": metrics["max_drawdown_pct_realized"],
        }
    summary: dict[str, object] = {
        "run_id": run_id,
        "strategy_version": STRATEGY_VERSION,
        "data_source": "MEXC USDT perpetual Futures public contract klines",
        "timeframe": "5m",
        "period_days": days,
        "start_inclusive_utc": start.isoformat(),
        "end_exclusive_utc": end.isoformat(),
        "warmup_start_inclusive_utc": fetch_start.isoformat(),
        "warmup_days": 8,
        "symbols": list(normalized_symbols),
        "candle_counts": {symbol: len(bars) for symbol, bars in data.items()},
        "data_coverage": coverage,
        "risk_model": {
            "fixed_gross_stop_risk_usd": settings.backtest_fixed_risk_usd,
            "gross_reward_by_target_r": {f"{rr:g}R": settings.backtest_fixed_risk_usd * rr for rr in TP_R_MULTIPLES},
            "note": "Dollar risk is before fees/slippage and may be reduced by the notional/leverage cap. A $5 risk with a $10 gross target is 2R, not 1.2R.",
        },
        "comparison_method": {
            "candle_fetch_passes": 1,
            "strategy_candidate_generation_passes": 1,
            "candidate_population_shared_across_scenarios": True,
            "targets_tested": [f"{rr:g}R" for rr in TP_R_MULTIPLES],
            "costs_included": True,
            "live_orders_placed": False,
        },
        "runtime_seconds": runtime_seconds,
        "scenarios": scenarios,
    }
    parameters: dict[str, object] = {
        "run_id": run_id,
        "period_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": list(normalized_symbols),
        "timeframe": "5m",
        "fixed_risk_usd": settings.backtest_fixed_risk_usd,
        "target_r_multiples": list(TP_R_MULTIPLES),
        "candidate_strategy_min_rr": 0.1,
        "fee_bps_per_side": settings.fee_bps_per_side,
        "slippage_bps_per_side": settings.slippage_bps_per_side,
        "funding_bps_per_8h": settings.funding_bps_per_8h,
        "max_concurrent_positions": settings.max_concurrent_positions,
    }
    store.save_backtest_run(
        run_id, STRATEGY_VERSION, parameters, summary,
        artifact_dir=f"telegram-command:{run_id}", started_at=started_at, completed_at=completed_at,
    )
    return summary


def format_rr_comparison(summary: dict[str, object]) -> str:
    """Render a compact Telegram-safe report of all four target scenarios."""
    symbols = ", ".join(str(s) for s in summary.get("symbols", []))
    period = summary.get("period_days", "?")
    risk_model = summary.get("risk_model", {})
    risk = risk_model.get("fixed_gross_stop_risk_usd", 5) if isinstance(risk_model, dict) else 5
    scenarios = summary.get("scenarios", {})
    lines = [
        "SHAYAN AI — MEXC STRATEGY BACKTEST",
        f"Period: {period}d | Symbols: {symbols} | Timeframe: 5m",
        f"Risk model: ${float(risk):.2f} gross risk per trade before costs/caps.",
        "Targets: 1R=$5, 1.2R=$6, 1.5R=$7.50, 2R=$10 gross reward.",
    ]
    if isinstance(scenarios, dict):
        reference_row = scenarios.get("1R")
        if isinstance(reference_row, dict):
            status_counts = reference_row.get("entry_status_counts", {})
            if isinstance(status_counts, dict):
                labels = {
                    "UNFILLED_EXPIRED": "expired-unfilled",
                    "UNFILLED_END_OF_DATA": "data-ended-unfilled",
                    "REJECTED_CAPACITY": "capacity-rejected",
                    "INVALID_GEOMETRY": "invalid-geometry",
                    "INVALID_SIZE": "invalid-size",
                    "TP": "TP",
                    "SL": "SL",
                    "OPEN_AT_END": "open-at-end",
                }
                status_parts = [
                    f"{labels.get(str(status), str(status).lower())}={int(count)}"
                    for status, count in sorted(status_counts.items()) if int(count) > 0
                ]
                lines.append("Entry outcomes (same across TP variants): " + (", ".join(status_parts) if status_parts else "no candidate status details available"))
            notes = reference_row.get("entry_status_notes", [])
            if isinstance(notes, list) and notes:
                # Keep Telegram output bounded and avoid repeating identical notes four times.
                lines.append("Entry note: " + str(notes[0])[:220])
        lines.append("")
        for label in ("1R", "1.2R", "1.5R", "2R"):
            row = scenarios.get(label)
            if not isinstance(row, dict):
                continue
            win_rate = row.get("win_rate_pct_resolved_denominator")
            pf = row.get("profit_factor_net_resolved")
            exp_r = row.get("expectancy_r_resolved")
            lines.extend([
                f"{label} TP — target ${float(row.get('gross_target_reward_usd_before_costs', 0)):.2f}",
                f"Signals/entered/resolved: {row.get('signals_total', 0)}/{row.get('entered_trades', 0)}/{row.get('resolved_trades', 0)}",
                f"W/L: {row.get('wins', 0)}/{row.get('losses', 0)} | Win rate: {_fmt_pct(win_rate)}",
                f"Net P&L: ${float(row.get('net_pnl_resolved') or 0):.2f} | PF: {_fmt_optional(pf)} | Expectancy: {_fmt_optional(exp_r)}R",
                f"Max DD: {_fmt_pct(row.get('max_drawdown_pct_realized'))}",
                "",
            ])
    lines.extend([
        f"Runtime: {summary.get('runtime_seconds', '?')}s | Data fetched once; setups generated once.",
        "Simulation only: no live orders. Results depend on OHLCV fill assumptions and do not prove future profitability.",
    ])
    # Metrics are not meaningful when there are no filled trades; label the run clearly.
    rows = summary.get("scenarios", {})
    if isinstance(rows, dict) and rows:
        entered_total = max(
            (int(row.get("entered_trades", 0)) for row in rows.values() if isinstance(row, dict)),
            default=0,
        )
        if entered_total == 0:
            lines.insert(4, "INCONCLUSIVE: no entries filled; TP performance cannot be compared from this run.")
    body = "\n".join(lines).strip()
    return body[:4000] if len(body) > 4000 else body


def _fmt_pct(value: object) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_optional(value: object) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
        return "n/a" if number != number or abs(number) == float("inf") else f"{number:.2f}"
    except (TypeError, ValueError):
        return "n/a"
