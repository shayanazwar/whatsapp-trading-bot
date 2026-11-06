from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.config import Settings
from app.indicators import ema, rsi_wilder
from app.mexc import CandleBatch, MexcClient
from app.models import Candle, ContractInfo
from app.sizing import calculate_quantity, round_price_to_tick
from app.strategy import StrategyConfig, signal_at

DirectionMode = Literal["BOTH", "LONG", "SHORT"]
RSI_CONFIGS = ((9, 65.0, 35.0), (9, 70.0, 30.0), (14, 65.0, 35.0), (14, 70.0, 30.0))


@dataclass(frozen=True)
class VariantResult:
    rsi_period: int
    upper: float
    lower: float
    tp_pct: float
    setups: int
    long_entries: int
    short_entries: int
    rejected_size: int
    stale_rejected: int
    tp_touches: int
    unresolved: int
    tp_subset_gross: float
    tp_subset_net: float
    fees: float
    average_hold_minutes: float | None
    average_contracts: float | None
    average_notional: float | None
    average_margin: float | None


def _format_dt(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _run_variant(
    candles: list[Candle], test_start_index: int, direction_mode: DirectionMode,
    config: StrategyConfig, tp_pct: float, contract: ContractInfo, settings: Settings,
    fast: list[float | None], mid: list[float | None], slow: list[float | None],
    rsi: list[float | None], pause_flags: list[bool] | None = None,
) -> VariantResult:
    setups = longs = shorts = rejected = stale_rejected = tp_touches = unresolved = 0
    gross_total = net_total = fees_total = 0.0
    holds: list[int] = []
    contracts_values: list[float] = []
    notionals: list[float] = []
    margins: list[float] = []
    # Precompute setup edges once, matching live false-to-true de-duplication.
    # Setups that form while a hypothetical backtest position is open are ignored,
    # but they still update the latch so a persistent condition is not re-entered.
    edge_signals = [None] * len(candles)
    previous_condition = None
    for index in range(max(test_start_index, 1), len(candles)):
        candidate = signal_at(candles, index, config, fast, mid, slow, rsi)
        current_condition = candidate.direction if candidate else None
        if candidate is not None and current_condition != previous_condition:
            if pause_flags is None or not pause_flags[index]:
                edge_signals[index] = candidate
        previous_condition = current_condition

    i = max(test_start_index, 1)
    slip = contract.price_tick * settings.backtest_slippage_ticks
    while i < len(candles) - 1:
        sig = edge_signals[i]
        if sig is None or (direction_mode != "BOTH" and sig.direction != direction_mode):
            i += 1
            continue
        setups += 1
        entry_bar = candles[i + 1]
        entry = entry_bar.open + slip if sig.direction == "LONG" else entry_bar.open - slip
        deviation = abs(entry / sig.price - 1.0) * 100.0
        if deviation > settings.signal_expiry_deviation_pct:
            stale_rejected += 1
            i += 1
            continue
        sizing = calculate_quantity(entry, contract, settings.max_margin_usdt, settings.model_leverage)
        if not sizing["valid"]:
            rejected += 1
            i += 1
            continue
        qty = float(sizing["contracts"])
        notional = float(sizing["notional"])
        margin = float(sizing["margin"])
        contracts_values.append(qty)
        notionals.append(notional)
        margins.append(margin)
        if sig.direction == "LONG":
            longs += 1
            target = round_price_to_tick(entry * (1.0 + tp_pct / 100.0), contract.price_tick, "LONG")
        else:
            shorts += 1
            target = round_price_to_tick(entry * (1.0 - tp_pct / 100.0), contract.price_tick, "SHORT")

        hit_index: int | None = None
        for j in range(i + 1, len(candles)):
            if sig.direction == "LONG" and candles[j].high >= target:
                hit_index = j
                break
            if sig.direction == "SHORT" and candles[j].low <= target:
                hit_index = j
                break
        if hit_index is None:
            unresolved += 1
            # A no-stop position remains open in this hypothetical lifecycle;
            # no further entry is permitted during this tested interval.
            break

        exit_price = target - slip if sig.direction == "LONG" else target + slip
        contract_size = contract.contract_size
        if sig.direction == "LONG":
            gross = (exit_price - entry) * qty * contract_size
        else:
            gross = (entry - exit_price) * qty * contract_size
        entry_notional = entry * qty * contract_size
        exit_notional = exit_price * qty * contract_size
        fees = (entry_notional + exit_notional) * settings.backtest_taker_fee_rate
        net = gross - fees
        gross_total += gross
        net_total += net
        fees_total += fees
        tp_touches += 1
        holds.append(hit_index - (i + 1) + 1)
        # A new setup may be acted on only after the TP candle has completed.
        i = hit_index + 1

    def avg(items: list[float] | list[int]) -> float | None:
        return sum(items) / len(items) if items else None

    return VariantResult(
        config.rsi_period, config.rsi_upper, config.rsi_lower, tp_pct, setups, longs, shorts,
        rejected, stale_rejected, tp_touches, unresolved, gross_total, net_total, fees_total,
        avg(holds), avg(contracts_values), avg(notionals), avg(margins),
    )


def _build_pause_flags(candles: list[Candle], settings: Settings) -> list[bool]:
    """Deterministic historical equivalent of the live abnormal-move gate."""
    flags = [False] * len(candles)
    paused = False
    recovery_start: int | None = None
    for i, candle in enumerate(candles):
        if i < 5 or candles[i - 5].close <= 0 or candle.close <= 0:
            flags[i] = paused
            continue
        move_5m = abs((candle.close / candles[i - 5].close - 1.0) * 100.0)
        range_1m = ((candle.high - candle.low) / candle.close) * 100.0
        if not paused:
            if move_5m >= settings.abnormal_5m_move_pct or range_1m >= settings.abnormal_1m_range_pct:
                paused = True
                recovery_start = None
        elif move_5m < settings.recovery_5m_move_pct and range_1m < settings.abnormal_1m_range_pct:
            if recovery_start is None:
                recovery_start = candle.time + 60
            elif (candle.time + 60) - recovery_start >= settings.recovery_minutes * 60:
                paused = False
                recovery_start = None
        else:
            recovery_start = None
        flags[i] = paused
    return flags


async def run_backtest(
    mexc: MexcClient, settings: Settings, days: int, direction_mode: DirectionMode, now: int,
) -> str:
    if days not in (1, 7, 30):
        raise ValueError("Supported backtest periods: 1d, 7d, 30d")
    if direction_mode not in ("BOTH", "LONG", "SHORT"):
        raise ValueError("Direction must be BOTH, LONG, or SHORT")
    latest_closed_open = ((now - 1) // 60) * 60 - 60
    test_bars = days * 24 * 60
    test_start = latest_closed_open - (test_bars - 1) * 60
    data_start = test_start - settings.warmup_candles * 60
    batch: CandleBatch = await mexc.get_candle_batch(data_start, latest_closed_open)
    if not batch.complete:
        missing_preview = ", ".join(_format_dt(x) for x in batch.missing_timestamps[:3]) or "none"
        return (
            f"BTCUSDT BACKTEST — INCOMPLETE DATA\nPeriod: {days}D | Direction: {direction_mode}\n"
            f"Requested candles: {batch.expected_count} | Received: {len(batch.candles)}\n"
            f"Missing: {len(batch.missing_timestamps)} | Duplicates: {len(batch.duplicate_timestamps)} | "
            f"Invalid: {len(batch.invalid_timestamps)} | Out-of-order: {len(batch.out_of_order_timestamps)} | "
            f"API segment errors: {len(batch.request_errors)}\n"
            f"First missing times: {missing_preview}\nNo performance metrics calculated. No candles were fabricated."
        )
    candles = batch.candles
    test_start_index = next((i for i, c in enumerate(candles) if c.time == test_start), None)
    if test_start_index is None:
        return "BACKTEST ABORTED: requested start candle missing after validation."

    contract = await mexc.get_contract_info()
    if contract.symbol != settings.mexc_symbol:
        return "BACKTEST ABORTED: contract metadata symbol mismatch."
    if contract.max_leverage > 0 and settings.model_leverage > contract.max_leverage:
        return (f"BACKTEST ABORTED: model leverage {settings.model_leverage:g}x exceeds "
                f"current contract metadata maximum {contract.max_leverage:g}x.")
    if contract.country_max_leverage > 0 and settings.model_leverage > contract.country_max_leverage:
        return (f"BACKTEST ABORTED: model leverage {settings.model_leverage:g}x exceeds "
                f"country-configured maximum {contract.country_max_leverage:g}x.")
    results: list[VariantResult] = []
    close_prices = [c.close for c in candles]
    fast_values = ema(close_prices, settings.ema_fast)
    mid_values = ema(close_prices, settings.ema_mid)
    slow_values = ema(close_prices, settings.ema_slow)
    pause_flags = _build_pause_flags(candles, settings)
    for rsi_period, upper, lower in RSI_CONFIGS:
        config_base = StrategyConfig(settings.ema_fast, settings.ema_mid, settings.ema_slow,
                                     rsi_period, upper, lower, settings.live_tp_move_pct)
        rsi_values = rsi_wilder(close_prices, rsi_period)
        for tp_pct in settings.backtest_tp_pcts:
            cfg = StrategyConfig(config_base.ema_fast, config_base.ema_mid, config_base.ema_slow,
                                 rsi_period, upper, lower, tp_pct)
            results.append(_run_variant(candles, test_start_index, direction_mode, cfg, tp_pct,
                                        contract, settings, fast_values, mid_values, slow_values, rsi_values,
                                        pause_flags))

    parts = [
        f"BTCUSDT BACKTEST — {days}D / {direction_mode}",
        f"Range: {_format_dt(test_start)} → {_format_dt(latest_closed_open)}",
        f"Candles downloaded: {len(candles)} (warm-up {settings.warmup_candles}) | Gaps: 0 | Source: MEXC Futures",
        f"Model: EMA {settings.ema_fast}/{settings.ema_mid}/{settings.ema_slow} | next 1m open ± {settings.backtest_slippage_ticks} tick",
        f"Size: max margin {settings.max_margin_usdt:.2f} USDT | model leverage {settings.model_leverage:g}x (account eligibility unverified; contract max {contract.max_leverage:g}x; country max {contract.country_max_leverage:g}x where supplied) | planning balance {settings.starting_balance_usdt:.2f} USDT (reference only; no compounding)",
        f"Fees: backtest assumes taker {settings.backtest_taker_fee_rate*100:.4f}% per side; public contract metadata currently reports {contract.taker_fee_rate*100:.4f}% (account fee tier may differ)",
        "NOTE: Hypothetical backtest only. No exchange orders or paper trades. TP_TOUCH is not a confirmed win.",
        "Liquidation/funding NOT MODELLED; unresolved trades may have liquidated. Win rate, PF, drawdown and loss streaks withheld.",
    ]
    for r in results:
        parts.append(
            f"\nRSI {r.rsi_period} ({r.upper:g}/{r.lower:g}) | TP {r.tp_pct:.2f}%\n"
            f"Setups: {r.setups} | LONG: {r.long_entries} | SHORT: {r.short_entries} | "
            f"Stale rejected: {r.stale_rejected} | Size rejected: {r.rejected_size}\n"
            f"TP touched: {r.tp_touches} | Unresolved/open at end: {r.unresolved}\n"
            f"TP-touch subset gross: {r.tp_subset_gross:+.4f} USDT | net after modelled fees/slippage: {r.tp_subset_net:+.4f} USDT\n"
            f"Fees on TP-touch subset: {r.fees:.4f} USDT | avg hold: {r.average_hold_minutes if r.average_hold_minutes is not None else 'N/A'} min\n"
            f"Avg contracts: {r.average_contracts if r.average_contracts is not None else 'N/A'} | "
            f"Avg notional: {r.average_notional if r.average_notional is not None else 'N/A'} USDT | "
            f"Avg margin used: {r.average_margin if r.average_margin is not None else 'N/A'} USDT"
        )
    parts.append("\nImportant: TP-touch subset PnL excludes unresolved outcomes; do not read it as total strategy PnL or evidence of profitability.")
    return "\n".join(parts)
