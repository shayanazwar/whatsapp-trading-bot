"""Conservative backtester for the selected 3-minute RSI/EMA LONG-only hypothesis.

No network access or live execution is performed by this module. It consumes verified candles.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.market.candles import Candle
from app.strategy.rsi_ema import RsiEmaConfig, evaluate_signals


@dataclass(frozen=True, slots=True)
class RsiEmaBacktestConfig:
    starting_equity: float = 100.0
    initial_margin: float = 1.0
    max_leverage: int = 50
    net_take_profit_usd: float = 0.30
    requested_stop_loss_usd: float = 3.0
    taker_fee_rate: float = 0.0008
    slippage_bps_per_side: float = 5.0
    maintenance_margin_rate: float = 0.005
    max_positions: int = 1
    assumed_funding_rate_per_8h: float = 0.0
    strategy: RsiEmaConfig = RsiEmaConfig()

    def validate(self) -> None:
        if self.starting_equity <= 0 or self.initial_margin <= 0:
            raise ValueError("Starting equity and initial margin must be positive")
        if not 1 <= self.max_leverage <= 50:
            raise ValueError("Leverage must be between 1 and 50")
        if self.net_take_profit_usd <= 0 or self.requested_stop_loss_usd <= 0:
            raise ValueError("Profit target and stop-loss request must be positive")
        if not 0 <= self.taker_fee_rate < 0.1 or not 0 <= self.slippage_bps_per_side <= 1000:
            raise ValueError("Invalid cost assumptions")
        if not 0 < self.maintenance_margin_rate < 0.5:
            raise ValueError("Invalid maintenance margin assumption")
        if self.max_positions != 1:
            raise ValueError("This release is configured for one global position")
        self.strategy.validate()


def _first(mapping: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        try:
            value = float(mapping.get(key))
        except (TypeError, ValueError, OverflowError):
            continue
        if value == value and abs(value) != float("inf"):
            return value
    return default


def _first_positive(mapping: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = _first(mapping, key, default=0.0)
        if value > 0:
            return value
    return default


def _funding_events_for_trade(events: Sequence[Mapping[str, Any]] | None, symbol: str,
                              entry_time: int, exit_time: int, notional: float,
                              default_rate_per_8h: float) -> float:
    if events:
        total = 0.0
        saw_event = False
        for event in events:
            stamp = int(_first(event, "settleTime", "fundingTime", "timestamp", "time", default=0))
            if stamp > 100_000_000_000:
                stamp //= 1000
            event_symbol = str(event.get("symbol", symbol)).upper().replace("-", "_")
            if event_symbol != symbol.upper().replace("-", "_") or not entry_time < stamp <= exit_time:
                continue
            rate = _first(event, "fundingRate", "funding_rate", "rate", default=0.0)
            total += notional * rate  # positive rate is a cost for a LONG
            saw_event = True
        if saw_event:
            return total
    if default_rate_per_8h and exit_time > entry_time:
        hours = (exit_time - entry_time) / 3600.0
        return notional * default_rate_per_8h * hours / 8.0
    return 0.0


def run_backtest_rsi_ema(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    window_days: int,
    config: RsiEmaBacktestConfig | None = None,
    contract_specs: Mapping[str, Mapping[str, Any]] | None = None,
    funding_events: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    cfg = config or RsiEmaBacktestConfig()
    cfg.validate()
    if window_days not in {1, 7, 30, 90}:
        raise ValueError("Backtest windows must be 1, 7, 30 or 90 days")
    all_bars: dict[str, list[Candle]] = {}
    for sym, items in candles_by_symbol.items():
        norm = str(sym).upper()
        bars = sorted((c for c in items if c.complete and c.timeframe == "3M"), key=lambda c: c.open_time)
        if bars:
            all_bars[norm] = bars
    if now_epoch is None:
        now_epoch = max((bs[-1].close_time for bs in all_bars.values()), default=0)
    cutoff = now_epoch - window_days * 86400
    eligible = {s: [b for b in bs if b.close_time > cutoff and b.close_time <= now_epoch] for s, bs in all_bars.items()}
    eligible = {s: bs for s, bs in eligible.items() if bs}
    warmup_start = {s: [b for b in all_bars[s] if b.close_time <= cutoff][-130:] + eligible[s] for s in eligible}
    candidates: list[tuple[int, str, int, Candle, Candle, dict[str, Any]]] = []
    for symbol, bars in warmup_start.items():
        signals = evaluate_signals(bars, cfg.strategy)
        index_to_signal = {s.candle_index: s for s in signals}
        # If early warm-up bars are included, only entries within the selected period count.
        for idx, signal in index_to_signal.items():
            if idx + 1 >= len(bars):
                continue
            sig_bar, entry_bar = bars[idx], bars[idx + 1]
            if signal.timestamp <= cutoff or entry_bar.open_time < cutoff or entry_bar.close_time > now_epoch:
                continue
            spec = (contract_specs or {}).get(symbol, {})
            if contract_specs is not None and symbol not in contract_specs:
                # Production backtests must not invent contract multipliers or quantity limits.
                continue
            spec_lev = _first_positive(spec, "countryConfigContractMaxLeverage", "maxLeverage", "max_leverage",
                                       default=float(cfg.max_leverage))
            max_lev = int(max(1, min(cfg.max_leverage, spec_lev)))
            candidates.append((entry_bar.open_time, symbol, idx, sig_bar, entry_bar,
                               {"signal": signal.as_dict(), "leverage": max_lev, "spec": dict(spec), "bars": bars}))
    candidates.sort(key=lambda x: (x[0], x[1]))

    equity = cfg.starting_equity
    peak_equity = equity
    max_drawdown = 0.0
    max_margin_used = 0.0
    ledger: list[dict[str, Any]] = []
    blocked_due_open_position = 0
    active_until = 0
    total_gross = total_fees = total_slippage = total_funding = 0.0
    tp_count = sl_count = liquidation_count = end_count = 0
    last_equity_points: list[dict[str, float | int]] = [{"timestamp": cutoff, "equity": equity}]
    slip = cfg.slippage_bps_per_side / 10000.0

    for entry_time, symbol, idx, signal_bar, entry_bar, context in candidates:
        if entry_time <= active_until:
            blocked_due_open_position += 1
            continue
        if equity <= 0 or equity < cfg.initial_margin:
            break
        spec = context["spec"]
        leverage = int(context["leverage"])
        contract_size = _first(spec, "contractSize", "contract_size", default=1.0)
        if contract_size <= 0:
            continue
        ref_entry = entry_bar.open
        entry_price = ref_entry * (1.0 + slip)  # market entry, adverse slippage
        target_notional = cfg.initial_margin * leverage
        # Size with MEXC contract multiplier and quantity step; reject impossible minimums.
        qty_raw = target_notional / (entry_price * contract_size)
        vol_unit = _first(spec, "volUnit", "volumeUnit", "quantityStep", default=1e-8)
        min_vol = _first(spec, "minVol", "minVolume", "minOrderQty", default=vol_unit)
        max_vol = _first(spec, "maxVol", "maxVolume", "maxOrderQty", default=float("inf"))
        if vol_unit <= 0 or min_vol <= 0 or max_vol <= 0:
            continue
        qty = math.floor((qty_raw + 1e-12) / vol_unit) * vol_unit
        digits = max(0, min(12, len(f"{vol_unit:.12f}".rstrip("0").split(".")[-1])))
        qty = round(qty, digits)
        if qty <= 0 or qty < min_vol or qty > max_vol:
            continue
        actual_notional = qty * entry_price * contract_size
        if actual_notional / leverage > cfg.initial_margin * 1.000001:
            continue
        actual_notional = qty * entry_price * contract_size
        entry_fee = actual_notional * cfg.taker_fee_rate
        # The requested $3 loss is retained as an explicit threshold, but isolated liquidation
        # is likely to occur first. The maintenance rate is an approximation until account data is verified.
        sl_price = entry_price - cfg.requested_stop_loss_usd / max(qty * contract_size, 1e-18)
        maintenance_rate = _first(spec, "maintenanceMarginRate", "maintenance_margin_rate", default=cfg.maintenance_margin_rate)
        if not 0 < maintenance_rate < 0.5:
            maintenance_rate = cfg.maintenance_margin_rate
        liquidation_price = entry_price * max(0.01, 1.0 - (1.0 / leverage - maintenance_rate))
        # Solve the market-trigger price for the configured net target after expected costs.
        needed_exit_fill = (cfg.net_take_profit_usd + qty * contract_size * entry_price * (1 + cfg.taker_fee_rate)) / max(qty * contract_size * (1 - cfg.taker_fee_rate), 1e-18)
        tp_price = needed_exit_fill / max(1.0 - slip, 1e-9)
        bars: list[Candle] = context["bars"]
        start_idx = idx + 1
        exit_reason = "DATA_WINDOW_END"
        exit_ref = bars[-1].close
        exit_time = bars[-1].close_time
        exit_bar = bars[-1]
        # Entry candle is evaluated because the candle opens after the signal was confirmed.
        # Conservative precedence: liquidation, stop, target if multiple levels touch in one bar.
        for future in bars[start_idx:]:
            if future.open_time < entry_time:
                continue
            if future.low <= liquidation_price:
                exit_reason, exit_ref, exit_time, exit_bar = "LIQUIDATION", liquidation_price, future.close_time, future
                break
            if future.low <= sl_price:
                exit_reason, exit_ref, exit_time, exit_bar = "STOP_LOSS", sl_price, future.close_time, future
                break
            if future.high >= tp_price:
                exit_reason, exit_ref, exit_time, exit_bar = "TAKE_PROFIT_NET_TARGET", tp_price, future.close_time, future
                break
        exit_price = exit_ref * (1.0 - slip)  # adverse market-exit slippage
        exit_notional = qty * exit_price * contract_size
        exit_fee = exit_notional * cfg.taker_fee_rate
        gross_pnl = qty * contract_size * (exit_ref - ref_entry)
        fees = entry_fee + exit_fee
        slip_cost = qty * contract_size * max(0.0, entry_price - ref_entry) + qty * contract_size * max(0.0, exit_ref - exit_price)
        funding_cost = _funding_events_for_trade((funding_events or {}).get(symbol), symbol,
            entry_time, exit_time, actual_notional, cfg.assumed_funding_rate_per_8h)
        pre_liquidation_net = gross_pnl - fees - funding_cost - slip_cost
        liquidation_adjustment = 0.0
        if exit_reason == "LIQUIDATION":
            # Conservative isolated-margin proxy: do not credit a liquidation with leaving unused margin.
            # Contract-specific settlement/liquidation fee remains unverified, so this is not an exchange quote.
            liquidation_adjustment = max(0.0, pre_liquidation_net + cfg.initial_margin)
        net_pnl = pre_liquidation_net - liquidation_adjustment
        if exit_reason == "TAKE_PROFIT_NET_TARGET" and net_pnl < cfg.net_take_profit_usd:
            # Price trigger touched but modeled net did not meet target; do not mislabel as target hit.
            exit_reason = "TARGET_COST_MISS"
        equity += net_pnl
        peak_equity = max(peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 1.0
        max_drawdown = max(max_drawdown, drawdown)
        max_margin_used = max(max_margin_used, cfg.initial_margin)
        total_gross += gross_pnl
        total_fees += fees
        total_slippage += slip_cost
        total_funding += funding_cost
        tp_count += int(exit_reason == "TAKE_PROFIT_NET_TARGET")
        sl_count += int(exit_reason == "STOP_LOSS")
        liquidation_count += int(exit_reason == "LIQUIDATION")
        end_count += int(exit_reason in {"DATA_WINDOW_END", "TARGET_COST_MISS"})
        ledger.append({
            "symbol": symbol, "direction": "LONG", "signal_time": signal_bar.close_time,
            "entry_time": entry_time, "exit_time": exit_time, "signal_reason": ";".join(context["signal"]["reason"]),
            "signal_close": signal_bar.close, "entry_reference_price": ref_entry, "entry_fill_price": entry_price,
            "exit_reference_price": exit_ref, "exit_fill_price": exit_price, "quantity": qty,
            "contract_size": contract_size, "leverage": leverage, "initial_margin": cfg.initial_margin,
            "notional": actual_notional, "rsi": context["signal"]["rsi"], "ema21": context["signal"]["ema21"],
            "ema50": context["signal"]["ema50"], "ema100": context["signal"]["ema100"],
            "gross_pnl": gross_pnl, "entry_fee": entry_fee, "exit_fee": exit_fee, "fees": fees,
            "funding_cost": funding_cost, "estimated_slippage_cost": slip_cost,
            "liquidation_adjustment": liquidation_adjustment, "net_pnl": net_pnl,
            "exit_reason": exit_reason, "holding_minutes": max(0.0, (exit_time - entry_time) / 60.0),
            "equity_after": equity, "drawdown_pct": drawdown * 100,
            "stop_requested_usd": cfg.requested_stop_loss_usd,
            "liquidation_precedes_requested_stop": liquidation_price > sl_price,
        })
        last_equity_points.append({"timestamp": exit_time, "equity": equity})
        active_until = exit_time

    winners = [x["net_pnl"] for x in ledger if x["net_pnl"] > 0]
    losers = [x["net_pnl"] for x in ledger if x["net_pnl"] < 0]
    gross_wins = sum(winners)
    gross_losses_abs = abs(sum(losers))
    net_total = equity - cfg.starting_equity
    count = len(ledger)
    profit_factor = (gross_wins / gross_losses_abs) if gross_losses_abs > 0 else None
    average_hold = sum(x["holding_minutes"] for x in ledger) / count if count else None
    data_available = any(bool(values) for values in eligible.values())
    report = {
        "status": "COMPLETED_WITH_DATA" if data_available else "NOT_RUN_NO_VERIFIED_BARS",
        "strategy": "RSI_OVERBOUGHT_EMA21_50_100_CROSS_LONG_ONLY_3M",
        "window": f"{window_days}D", "window_days": window_days, "data_start": cutoff, "data_end": now_epoch,
        "starting_equity": cfg.starting_equity, "ending_equity": equity,
        "gross_pnl": total_gross, "trading_fees": total_fees, "funding_costs": total_funding,
        "estimated_slippage_cost": total_slippage, "liquidation_adjustment": sum(float(x["liquidation_adjustment"]) for x in ledger),
        "net_profit": net_total,
        "net_return_pct": net_total / cfg.starting_equity * 100,
        "trades_opened": count, "trades_closed": count, "completed_trades": count,
        "long_count": count, "short_count": 0, "wins": len(winners), "losses": len(losers),
        "breakeven": count - len(winners) - len(losers), "win_rate_pct": len(winners) / count * 100 if count else None,
        "average_winning_trade": sum(winners) / len(winners) if winners else None,
        "average_losing_trade": sum(losers) / len(losers) if losers else None,
        "profit_factor": profit_factor, "expectancy_per_trade": net_total / count if count else None,
        "max_drawdown_pct": max_drawdown * 100, "average_holding_minutes": average_hold,
        "max_holding_minutes": max((x["holding_minutes"] for x in ledger), default=None),
        "take_profit_exits": tp_count, "stop_loss_exits": sl_count, "liquidation_exits": liquidation_count,
        "end_of_window_or_target_cost_miss_exits": end_count, "max_concurrent_positions": 1 if ledger else 0,
        "peak_margin_used": max_margin_used, "blocked_signals_due_to_open_position": blocked_due_open_position,
        "per_symbol": _per_symbol(ledger), "trade_ledger": ledger, "equity_curve": last_equity_points,
        "data_quality": {"symbols_with_bars": list(eligible), "bar_counts": {s: len(b) for s, b in eligible.items()},
            "warmup_bars_used": {s: max(0, len(warmup_start[s]) - len(eligible[s])) for s in eligible},
            "historical_funding_events_supplied": bool(funding_events),
            "limitations": ["Synthetic fills; no exchange fills or order-book replay.",
                "MEXC contract-specific liquidation and fees need account/contract verification.",
                "Intrabar ordering is unknowable from OHLC; liquidation and stops take precedence over targets.",
                "The $3 monetary stop can sit beyond estimated liquidation for a $1-margin position.",
                "Historical spread/depth and intrabar market impact are not available in candle-only data."]},
        "assumptions": {**asdict(cfg), "strategy": asdict(cfg.strategy), "taker_fee_rate_per_side": cfg.taker_fee_rate,
            "slippage_bps_per_side": cfg.slippage_bps_per_side, "isolated_margin": True,
            "actual_live_orders": False},
    }
    return report


def _per_symbol(ledger: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for trade in ledger:
        symbol = str(trade["symbol"])
        row = result.setdefault(symbol, {"trades": 0, "wins": 0, "net_pnl": 0.0, "fees": 0.0,
                                         "gross_pnl": 0.0, "liquidations": 0})
        row["trades"] += 1
        row["wins"] += int(float(trade["net_pnl"]) > 0)
        row["net_pnl"] += float(trade["net_pnl"])
        row["fees"] += float(trade["fees"])
        row["gross_pnl"] += float(trade["gross_pnl"])
        row["liquidations"] += int(trade["exit_reason"] == "LIQUIDATION")
    return result


def write_backtest_artifacts(report: Mapping[str, Any], output_dir: str | Path, *, prefix: str) -> dict[str, str]:
    """Write a machine-readable report and detailed trade ledger without fabricating data."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{prefix}.json"
    csv_path = out / f"{prefix}_trades.csv"
    txt_path = out / f"{prefix}.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False, default=str), encoding="utf-8")
    ledger = list(report.get("trade_ledger", []))
    columns = list(ledger[0].keys()) if ledger else ["symbol", "direction", "signal_time", "entry_time", "exit_time", "net_pnl", "exit_reason"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ledger)
    win_rate_text = "n/a" if report["win_rate_pct"] is None else f"{report['win_rate_pct']:.2f}%"
    if report.get("status") == "NOT_RUN_NO_VERIFIED_BARS":
        lines = [
            f"3-MIN RSI/EMA BACKTEST — {report['window']}",
            "STATUS: NOT RUN — no verified historical candles were available.",
            "No performance result is reported. Do not interpret placeholder equity fields as a zero-return backtest.",
            "LIVE TRADING: DISABLED.",
            "Data-quality and configuration details are recorded in the JSON report.",
        ]
    else:
        lines = [
            f"3-MIN RSI/EMA BACKTEST — {report['window']}",
            f"Starting equity: ${report['starting_equity']:.2f}",
            f"Ending equity: ${report['ending_equity']:.2f}",
            f"Net P&L: ${report['net_profit']:.2f} ({report['net_return_pct']:.2f}%)",
            f"Trades: {report['completed_trades']} | Wins/Losses: {report['wins']}/{report['losses']} | LONG only",
            f"Fees: ${report['trading_fees']:.4f} | Funding: ${report['funding_costs']:.4f} | Estimated slippage: ${report['estimated_slippage_cost']:.4f}",
            f"Win rate: {win_rate_text} | Profit factor: {report['profit_factor']}",
            f"Max drawdown: {report['max_drawdown_pct']:.2f}% | TP exits: {report['take_profit_exits']} | SL exits: {report['stop_loss_exits']} | Liquidations: {report['liquidation_exits']}",
            "LIVE TRADING: DISABLED. Historical candle simulation is not a guarantee of future results.",
            "Known limitation: $3 stop is not a guaranteed maximum loss for $1 margin; liquidation may happen first.",
            "Data-quality limitations are recorded in the JSON report.",
        ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "text": str(txt_path)}


def render_equity_curve(report: Mapping[str, Any], output_path: str | Path) -> Path:
    """Render the actual simulated equity points from a completed report."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from datetime import datetime, timezone

    points = list(report.get("equity_curve", []))
    if not report.get("data_quality", {}).get("symbols_with_bars"):
        points = []
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4.5), dpi=140)
    if points:
        dates = [datetime.fromtimestamp(int(p["timestamp"]), tz=timezone.utc) for p in points]
        values = [float(p["equity"]) for p in points]
        ax.plot(dates, values, linewidth=1.3, label="Simulated equity")
        ax.axhline(float(report["starting_equity"]), linestyle="--", linewidth=0.8, label="Starting equity")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d %H:%M", tz=timezone.utc))
        ax.legend(loc="best")
    else:
        ax.text(0.5, 0.5, "No verified historical candles; no equity series generated",
                ha="center", va="center", transform=ax.transAxes)
    ax.set_title(f"RSI/EMA Backtest Equity — {report.get('window', '')} (simulated)")
    ax.set_ylabel("Equity (USDT)")
    ax.set_xlabel("Time (UTC)")
    ax.grid(True, alpha=0.2)
    fig.autofmt_xdate()
    fig.tight_layout()
    try:
        fig.savefig(out, format="png")
    finally:
        plt.close(fig)
    return out
