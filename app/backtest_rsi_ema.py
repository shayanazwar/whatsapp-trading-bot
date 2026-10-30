"""Cross-margin-aware 3-minute RSI/EMA backtester.

This is a historical simulation, not an exchange execution engine. It consumes completed
MEXC trade candles, optional fair-price candles, contract metadata and funding history.
No live order endpoint is called.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
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
    liquidation_fee_rate: float = 0.0
    margin_mode: str = "cross"
    max_positions: int = 1
    max_holding_minutes: int = 1440
    assumed_funding_rate_per_8h: float = 0.0
    strategy: RsiEmaConfig = RsiEmaConfig()

    def validate(self) -> None:
        if not math.isfinite(self.starting_equity) or self.starting_equity <= 0:
            raise ValueError("Starting equity must be positive and finite")
        if not math.isfinite(self.initial_margin) or self.initial_margin <= 0:
            raise ValueError("Initial margin must be positive and finite")
        if not 1 <= self.max_leverage <= 50:
            raise ValueError("Leverage must be between 1 and 50")
        if self.net_take_profit_usd <= 0 or self.requested_stop_loss_usd <= 0:
            raise ValueError("Profit target and stop-loss request must be positive")
        if not 0 <= self.taker_fee_rate < 0.1 or not 0 <= self.slippage_bps_per_side <= 1000:
            raise ValueError("Invalid fee or slippage assumptions")
        if not 0 < self.maintenance_margin_rate < 0.5:
            raise ValueError("Invalid maintenance margin assumption")
        if not 0 <= self.liquidation_fee_rate < 0.1:
            raise ValueError("Invalid liquidation fee assumption")
        if self.margin_mode not in {"cross", "isolated"}:
            raise ValueError("margin_mode must be cross or isolated")
        if self.max_positions != 1:
            raise ValueError("This backtest currently models one global position at a time")
        if self.max_holding_minutes < 3:
            raise ValueError("max_holding_minutes must be at least one 3-minute candle")
        self.strategy.validate()


def _number(mapping: Mapping[str, Any] | None, *keys: str, default: float = 0.0) -> float:
    if not mapping:
        return default
    for key in keys:
        try:
            value = float(mapping.get(key))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            return value
    return default


def _positive(mapping: Mapping[str, Any] | None, *keys: str, default: float = 0.0) -> float:
    value = _number(mapping, *keys, default=default)
    return value if value > 0 else default


def _funding_events_for_trade(
    events: Sequence[Mapping[str, Any]] | None,
    symbol: str,
    entry_time: int,
    exit_time: int,
    notional: float,
    default_rate_per_8h: float,
    direction: str,
    *,
    quantity_units: float | None = None,
    fair_price_bars: Sequence[Candle] | None = None,
) -> float:
    """Return funding paid (positive) or received (negative); positive rate favours shorts.

    When fair-price bars are available, value each settlement at the latest completed
    fair-price candle close at or before the settlement timestamp. Entry notional is the
    documented fallback when that historical mark is unavailable.
    """
    if events:
        total = 0.0
        saw_event = False
        direction_sign = 1.0 if direction == "LONG" else -1.0
        for event in events:
            stamp = int(_number(event, "settleTime", "fundingTime", "timestamp", "time", default=0))
            if stamp > 100_000_000_000:
                stamp //= 1000
            event_symbol = str(event.get("symbol", symbol)).upper().replace("-", "_")
            if event_symbol != symbol.upper().replace("-", "_") or not entry_time < stamp <= exit_time:
                continue
            rate = _number(event, "fundingRate", "funding_rate", "rate", default=0.0)
            settlement_notional = notional
            if quantity_units and fair_price_bars:
                mark_bar = next(
                    (bar for bar in reversed(fair_price_bars) if bar.close_time <= stamp),
                    None,
                )
                if mark_bar is not None:
                    settlement_notional = quantity_units * mark_bar.close
            total += settlement_notional * rate * direction_sign
            saw_event = True
        if saw_event:
            return total
    if default_rate_per_8h and exit_time > entry_time:
        hours = (exit_time - entry_time) / 3600.0
        direction_sign = 1.0 if direction == "LONG" else -1.0
        return notional * default_rate_per_8h * hours / 8.0 * direction_sign
    return 0.0


def _cross_liquidation_price(
    *, direction: str,
    entry_price: float,
    unit_quantity: float,
    wallet_balance: float,
    maintenance_rate: float,
    liquidation_fee_rate: float,
    isolated_margin: float,
) -> float | None:
    """Apply MEXC's published single-position cross/isolated maintenance-margin equation.

    For cross mode, wallet_balance is the account's cross wallet balance after entry fees.
    Liquidation fee is solved on the estimated liquidation notional. This model assumes no
    other open positions, isolated positions or order margin. MEXC's
    live liquidation trigger uses Fair Price; callers should provide fair-price OHLC bars.
    """
    if unit_quantity <= 0 or entry_price <= 0:
        return None
    entry_notional = entry_price * unit_quantity
    maintenance = entry_notional * maintenance_rate
    liquidation_fee = entry_notional * liquidation_fee_rate
    collateral = wallet_balance if isolated_margin <= 0 else isolated_margin
    if direction == "LONG":
        # Solve wallet + unrealized P&L = maintenance margin + fee charged
        # against the liquidation notional (rather than entry notional).
        denominator = unit_quantity * (1.0 - liquidation_fee_rate)
        if denominator <= 0:
            return None
        price = (entry_notional * (1.0 + maintenance_rate) - collateral) / denominator
    else:
        denominator = unit_quantity * (1.0 + liquidation_fee_rate)
        price = (collateral + entry_notional * (1.0 - maintenance_rate)) / denominator
    return price if math.isfinite(price) and price > 0 else None


def _net_exit_pnl(
    *,
    direction: str,
    ref_entry: float,
    entry_fill: float,
    exit_ref: float,
    quantity_units: float,
    fee_rate: float,
    slippage_rate: float,
    funding_cost: float = 0.0,
    liquidation_fee_rate: float = 0.0,
) -> dict[str, float]:
    sign = 1.0 if direction == "LONG" else -1.0
    exit_fill = exit_ref * (1.0 - slippage_rate if direction == "LONG" else 1.0 + slippage_rate)
    gross = quantity_units * (exit_ref - ref_entry) * sign
    entry_fee = quantity_units * entry_fill * fee_rate
    exit_fee = quantity_units * exit_fill * fee_rate
    slippage_cost = quantity_units * (abs(entry_fill - ref_entry) + abs(exit_fill - exit_ref))
    liquidation_fee = quantity_units * exit_ref * liquidation_fee_rate
    net = gross - entry_fee - exit_fee - slippage_cost - funding_cost - liquidation_fee
    return {
        "exit_fill": exit_fill, "gross_pnl": gross, "entry_fee": entry_fee,
        "exit_fee": exit_fee, "fees": entry_fee + exit_fee,
        "slippage_cost": slippage_cost, "funding_cost": funding_cost,
        "liquidation_fee": liquidation_fee, "net_pnl": net,
    }


def _stop_trigger_price(
    *,
    direction: str,
    reference_entry: float,
    quantity_units: float,
    net_loss_limit: float,
    fee_rate: float,
    slippage_rate: float,
) -> float:
    """Solve the stop trigger so the modeled loss includes fees and slippage."""
    if direction == "LONG":
        numerator = reference_entry * (1.0 + slippage_rate) * (1.0 + fee_rate) - net_loss_limit / quantity_units
        denominator = (1.0 - slippage_rate) * (1.0 - fee_rate)
        return numerator / denominator
    numerator = reference_entry * (1.0 - slippage_rate) * (1.0 - fee_rate) + net_loss_limit / quantity_units
    denominator = (1.0 + slippage_rate) * (1.0 + fee_rate)
    return numerator / denominator


def _round_trigger_to_tick(price: float, tick: float, direction: str, order_type: str) -> float:
    """Round trigger prices with decimal arithmetic to the contract price tick."""
    if tick <= 0:
        return price
    units = Decimal(str(price)) / Decimal(str(tick))
    if direction == "LONG":
        rounding = ROUND_CEILING if order_type == "STOP" else ROUND_FLOOR
    else:
        rounding = ROUND_FLOOR if order_type == "STOP" else ROUND_CEILING
    rounded_units = units.to_integral_value(rounding=rounding)
    return max(tick, float(rounded_units * Decimal(str(tick))))


def _target_trigger_price(
    *, direction: str,
    reference_entry: float,
    quantity_units: float,
    net_target: float,
    fee_rate: float,
    slippage_rate: float,
) -> float:
    """Solve the trigger price for a net target after both fees and both slippage legs."""
    if direction == "LONG":
        numerator = net_target / quantity_units + reference_entry * (
            1.0 + fee_rate * (1.0 + slippage_rate) + slippage_rate
        )
        denominator = 1.0 - fee_rate * (1.0 - slippage_rate) - slippage_rate
        return numerator / denominator
    numerator = reference_entry * (1.0 - slippage_rate) * (1.0 - fee_rate) - net_target / quantity_units
    denominator = (1.0 + slippage_rate) * (1.0 + fee_rate)
    return numerator / denominator


def _max_losing_streak(trades: Sequence[Mapping[str, Any]]) -> int:
    current = maximum = 0
    for trade in trades:
        if float(trade.get("net_pnl", 0.0)) < -1e-12:
            current += 1
            maximum = max(maximum, current)
        elif float(trade.get("net_pnl", 0.0)) > 1e-12:
            current = 0
    return maximum



def _risk_tier_terms(
    spec: Mapping[str, Any],
    *,
    direction: str,
    quantity: float,
    notional: float,
    base_mmr: float,
    contract_max_leverage: int,
) -> dict[str, Any]:
    """Resolve MEXC contract risk tier when the public contract metadata supports it.

    INCREASE mode uses the published base volume, increment volume and MMR/IMR
    increments. CUSTOM mode uses the published maxVol/mmr/imr/maxLeverage table.
    If tier metadata is absent, the caller must disclose that only base MMR was used.
    """
    risk_type = str(spec.get("riskLimitType", "BY_VOLUME")).upper()
    basis = notional if risk_type == "BY_VALUE" else quantity
    mode = str(spec.get("riskLimitMode", "")).upper()

    if mode == "CUSTOM":
        custom = spec.get("riskLimitCustom")
        if not isinstance(custom, list) or not custom:
            return {"status": "UNKNOWN", "mmr": base_mmr, "max_leverage": contract_max_leverage, "tier": None}
        valid = [row for row in custom if isinstance(row, Mapping) and _positive(row, "maxVol") > 0]
        valid.sort(key=lambda row: _positive(row, "maxVol"))
        selected = next((row for row in valid if basis <= _positive(row, "maxVol")), None)
        if selected is None:
            return {"status": "OVER_LIMIT", "mmr": base_mmr, "max_leverage": 0, "tier": None}
        tier_mmr = _positive(selected, "mmr", default=base_mmr)
        tier_imr = _positive(selected, "imr", default=0.0)
        tier_leverage = int(_positive(selected, "maxLeverage", default=contract_max_leverage))
        if tier_imr > 0:
            tier_leverage = min(tier_leverage, max(1, int(math.floor(1.0 / tier_imr))))
        return {
            "status": "VERIFIED", "mmr": tier_mmr,
            "max_leverage": max(1, min(contract_max_leverage, tier_leverage)),
            "tier": int(_positive(selected, "level", default=0)) or None,
            "risk_limit_type": risk_type,
        }

    if mode == "INCREASE":
        side = "Long" if direction == "LONG" else "Short"
        split_sides = int(_number(spec, "riskLongShortSwitch", default=0)) == 1
        base_keys = (f"riskBaseVol{side}", "riskBaseVol") if split_sides else ("riskBaseVol",)
        increment_keys = (f"riskIncrVol{side}", "riskIncrVol") if split_sides else ("riskIncrVol",)
        base_volume = _positive(spec, *base_keys, default=0.0)
        increment_volume = _positive(spec, *increment_keys, default=0.0)
        level_limit = int(_positive(spec, "riskLevelLimit", default=0))
        if base_volume <= 0 or level_limit <= 0:
            return {"status": "UNKNOWN", "mmr": base_mmr, "max_leverage": contract_max_leverage, "tier": None}
        if basis <= base_volume:
            tier = 1
        elif increment_volume > 0:
            tier = int(math.ceil((basis - base_volume) / increment_volume)) + 1
        else:
            return {"status": "OVER_LIMIT", "mmr": base_mmr, "max_leverage": 0, "tier": None}
        if tier > level_limit:
            return {"status": "OVER_LIMIT", "mmr": base_mmr, "max_leverage": 0, "tier": tier}
        mmr_increment = max(0.0, _number(spec, "riskIncrMmr", default=0.0))
        imr_base = _positive(spec, "initialMarginRate", default=0.0)
        imr_increment = max(0.0, _number(spec, "riskIncrImr", default=0.0))
        tier_imr = imr_base + (tier - 1) * imr_increment if imr_base > 0 else 0.0
        tier_leverage = contract_max_leverage
        if tier_imr > 0:
            tier_leverage = min(tier_leverage, max(1, int(math.floor(1.0 / tier_imr))))
        return {
            "status": "VERIFIED",
            "mmr": base_mmr + (tier - 1) * mmr_increment,
            "max_leverage": tier_leverage, "tier": tier,
            "risk_limit_type": risk_type,
        }

    return {"status": "UNKNOWN", "mmr": base_mmr, "max_leverage": contract_max_leverage, "tier": None}


def run_backtest_rsi_ema(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    window_days: int,
    config: RsiEmaBacktestConfig | None = None,
    contract_specs: Mapping[str, Mapping[str, Any]] | None = None,
    funding_events: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    liquidation_candles_by_symbol: Mapping[str, Sequence[Candle]] | None = None,
    funding_price_candles_by_symbol: Mapping[str, Sequence[Candle]] | None = None,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    cfg = config or RsiEmaBacktestConfig()
    cfg.validate()
    if window_days not in {1, 7, 30, 60, 90}:
        raise ValueError("Backtest windows must be 1, 7, 30, 60 or 90 days")

    all_bars: dict[str, list[Candle]] = {}
    for symbol, items in candles_by_symbol.items():
        bars = sorted(
            (bar for bar in items if bar.complete and bar.timeframe == "3M"),
            key=lambda bar: bar.open_time,
        )
        if bars:
            all_bars[str(symbol).upper()] = bars
    if now_epoch is None:
        now_epoch = max((bars[-1].close_time for bars in all_bars.values()), default=0)
    cutoff = now_epoch - window_days * 86400
    eligible = {
        symbol: [bar for bar in bars if cutoff < bar.close_time <= now_epoch]
        for symbol, bars in all_bars.items()
    }
    eligible = {symbol: bars for symbol, bars in eligible.items() if bars}
    # Preserve every fetched completed warm-up candle. Truncating this to 130 bars
    # biases EMA100 near the start of the test; command loading provides 450 warm-up bars.
    warmup = {
        symbol: [bar for bar in all_bars[symbol] if bar.close_time <= cutoff] + eligible[symbol]
        for symbol in eligible
    }

    fair_by_symbol: dict[str, dict[int, Candle]] = {}
    fair_data_symbols: list[str] = []
    funding_price_by_symbol: dict[str, list[Candle]] = {
        str(symbol).upper(): sorted(list(bars), key=lambda bar: bar.close_time)
        for symbol, bars in (funding_price_candles_by_symbol or {}).items()
    }
    if liquidation_candles_by_symbol is not None:
        for symbol, bars in liquidation_candles_by_symbol.items():
            mapping = {
                bar.open_time: bar for bar in bars
                if bar.complete and bar.timeframe == "3M" and bar.symbol == symbol
            }
            if mapping:
                fair_by_symbol[str(symbol).upper()] = mapping
                fair_data_symbols.append(str(symbol).upper())

    candidates: list[tuple[int, str, int, Candle, Candle, dict[str, Any]]] = []
    signal_count = long_signals = short_signals = 0
    for symbol, bars in warmup.items():
        strategy_signals = evaluate_signals(bars, cfg.strategy)
        for signal in strategy_signals:
            idx = signal.candle_index
            if signal.timestamp <= cutoff or signal.timestamp > now_epoch:
                continue
            # Count every completed-candle signal in the requested window, even when
            # the final signal has no subsequent completed entry candle yet.
            signal_count += 1
            long_signals += int(signal.direction == "LONG")
            short_signals += int(signal.direction == "SHORT")
            if idx + 1 >= len(bars):
                continue
            signal_bar, entry_bar = bars[idx], bars[idx + 1]
            if entry_bar.open_time < cutoff or entry_bar.close_time > now_epoch:
                continue
            spec = (contract_specs or {}).get(symbol, {})
            if contract_specs is not None and symbol not in contract_specs:
                continue
            spec_leverage = _positive(
                spec, "countryConfigContractMaxLeverage", "maxLeverage", "max_leverage",
                default=float(cfg.max_leverage),
            )
            leverage = int(max(1, min(cfg.max_leverage, spec_leverage)))
            candidates.append((
                entry_bar.open_time, symbol, idx, signal_bar, entry_bar,
                {"signal": signal.as_dict(), "leverage": leverage, "spec": dict(spec), "bars": bars},
            ))
    candidates.sort(key=lambda item: (item[0], item[1]))

    equity = cfg.starting_equity
    peak_equity = equity
    max_drawdown_pct = 0.0
    ledger: list[dict[str, Any]] = []
    blocked_due_open_position = 0
    active_until = 0
    gross_total = fees_total = slippage_total = funding_total = liquidation_fees_total = 0.0
    peak_margin_used = 0.0
    fee_fallback_symbols: set[str] = set()
    nonstandard_fee_mode_symbols: set[str] = set()
    liquidation_fee_fallback_symbols: set[str] = set()
    risk_tier_unknown_symbols: set[str] = set()
    risk_tier_rejected_signals = 0
    risk_tiers_used: dict[str, set[int]] = {}
    unresolved_count = 0
    unrealised_total = 0.0
    equity_curve: list[dict[str, float | int]] = [{"timestamp": cutoff, "equity": equity}]
    slip = cfg.slippage_bps_per_side / 10000.0
    liquidations = 0
    expiries = 0

    for entry_time, symbol, idx, signal_bar, entry_bar, context in candidates:
        if entry_time <= active_until:
            blocked_due_open_position += 1
            continue
        if equity <= 0 or equity < cfg.initial_margin:
            break

        spec = context["spec"]
        direction = str(context["signal"]["direction"])
        leverage = int(context["leverage"])
        contract_size = _positive(spec, "contractSize", "contract_size", default=0.0)
        vol_step = _positive(spec, "volUnit", "volumeUnit", "quantityStep", default=0.0)
        min_vol = _positive(spec, "minVol", "minVolume", "minOrderQty", default=vol_step)
        max_vol = _positive(spec, "maxVol", "maxVolume", "maxOrderQty", default=float("inf"))
        if min(contract_size, vol_step, min_vol) <= 0 or max_vol < min_vol:
            continue

        reference_entry = entry_bar.open
        entry_fill = reference_entry * (1.0 + slip if direction == "LONG" else 1.0 - slip)
        contract_fee_raw = _number(spec, "takerFeeRate", "taker_fee_rate", default=-1.0)
        contract_fee_rate = contract_fee_raw
        if not 0 <= contract_fee_rate < 0.1:
            contract_fee_rate = cfg.taker_fee_rate
            fee_fallback_symbols.add(symbol)
        fee_mode = str(spec.get("feeRateMode", "NORMAL")).upper()
        if fee_mode not in {"", "NORMAL", "BASE"}:
            nonstandard_fee_mode_symbols.add(symbol)
        price_tick = _positive(spec, "priceUnit", "price_unit", default=0.0)
        if price_tick <= 0:
            # Backtest contract validation normally rejects this; retain a fail-safe
            # for direct library callers that bypass command-layer validation.
            continue
        precision = max(0, min(12, len(f"{vol_step:.12f}".rstrip("0").split(".")[-1])))
        contract_max_leverage = leverage
        quantity = quantity_units = notional = actual_initial_margin = 0.0
        tier_terms: dict[str, Any] = {"status": "UNKNOWN", "mmr": 0.0, "max_leverage": leverage, "tier": None}
        for _ in range(5):
            target_notional = cfg.initial_margin * leverage
            quantity_raw = target_notional / (entry_fill * contract_size)
            quantity = round(math.floor((quantity_raw + 1e-12) / vol_step) * vol_step, precision)
            if quantity <= 0 or quantity < min_vol or quantity > max_vol:
                break
            quantity_units = quantity * contract_size
            notional = quantity_units * entry_fill
            tier_terms = _risk_tier_terms(
                spec, direction=direction, quantity=quantity, notional=notional,
                base_mmr=_positive(spec, "maintenanceMarginRate", "maintenance_margin_rate", "mmr", default=cfg.maintenance_margin_rate),
                contract_max_leverage=contract_max_leverage,
            )
            if tier_terms["status"] == "OVER_LIMIT":
                risk_tier_rejected_signals += 1
                quantity = 0.0
                break
            tier_max_leverage = int(tier_terms.get("max_leverage") or contract_max_leverage)
            if leverage > tier_max_leverage:
                leverage = max(1, tier_max_leverage)
                continue
            actual_initial_margin = notional / leverage
            break
        else:
            continue
        if quantity <= 0 or actual_initial_margin <= 0:
            continue
        if actual_initial_margin > cfg.initial_margin * 1.000001 or actual_initial_margin > equity:
            continue
        if tier_terms["status"] == "UNKNOWN":
            risk_tier_unknown_symbols.add(symbol)
        elif tier_terms.get("tier") is not None:
            risk_tiers_used.setdefault(symbol, set()).add(int(tier_terms["tier"]))
        entry_fee = notional * contract_fee_rate
        wallet_after_entry_fee = equity - entry_fee
        maintenance_rate = _positive(
            {"mmr": tier_terms.get("mmr", 0.0)}, "mmr",
            default=_positive(spec, "maintenanceMarginRate", "maintenance_margin_rate", "mmr", default=cfg.maintenance_margin_rate),
        )
        if not 0 < maintenance_rate < 0.5:
            maintenance_rate = cfg.maintenance_margin_rate
        liquidation_fee_raw = _number(
            spec, "liquidationFeeRate", "liquidation_fee_rate", default=-1.0,
        )
        liquidation_fee_rate = liquidation_fee_raw
        if not 0 <= liquidation_fee_rate < 0.1:
            liquidation_fee_rate = cfg.liquidation_fee_rate
            liquidation_fee_fallback_symbols.add(symbol)
        isolated_margin = actual_initial_margin if cfg.margin_mode == "isolated" else 0.0
        liquidation_price = _cross_liquidation_price(
            direction=direction, entry_price=entry_fill, unit_quantity=quantity_units,
            wallet_balance=wallet_after_entry_fee, maintenance_rate=maintenance_rate,
            liquidation_fee_rate=liquidation_fee_rate, isolated_margin=isolated_margin,
        )

        stop_price = _stop_trigger_price(
            direction=direction, reference_entry=reference_entry, quantity_units=quantity_units,
            net_loss_limit=cfg.requested_stop_loss_usd, fee_rate=contract_fee_rate,
            slippage_rate=slip,
        )
        target_price = _target_trigger_price(
            direction=direction, reference_entry=reference_entry, quantity_units=quantity_units,
            net_target=cfg.net_take_profit_usd, fee_rate=contract_fee_rate, slippage_rate=slip,
        )
        stop_price = _round_trigger_to_tick(stop_price, price_tick, direction, "STOP")
        target_price = _round_trigger_to_tick(target_price, price_tick, direction, "TARGET")
        if stop_price <= 0 or target_price <= 0:
            continue
        if direction == "LONG" and not stop_price < reference_entry < target_price:
            continue
        if direction == "SHORT" and not target_price < reference_entry < stop_price:
            continue

        # Actual fill RR uses expected net target divided by the modeled net loss at the planned SL.
        stop_outcome = _net_exit_pnl(
            direction=direction, ref_entry=reference_entry, entry_fill=entry_fill,
            exit_ref=stop_price, quantity_units=quantity_units, fee_rate=contract_fee_rate,
            slippage_rate=slip,
        )
        expected_stop_loss = abs(min(0.0, stop_outcome["net_pnl"]))
        target_outcome = _net_exit_pnl(
            direction=direction, ref_entry=reference_entry, entry_fill=entry_fill,
            exit_ref=target_price, quantity_units=quantity_units, fee_rate=contract_fee_rate,
            slippage_rate=slip,
        )
        expected_target_profit = max(0.0, target_outcome["net_pnl"])
        if expected_stop_loss <= 0 or expected_target_profit <= 0:
            continue
        actual_fill_rr = expected_target_profit / expected_stop_loss
        signal_rr = cfg.net_take_profit_usd / cfg.requested_stop_loss_usd

        bars: list[Candle] = context["bars"]
        fair_map = fair_by_symbol.get(symbol, {})
        start_index = idx + 1
        exit_reason: str | None = None
        exit_reference = 0.0
        exit_time = 0
        exit_bar: Candle | None = None
        mae_usd = 0.0
        mfe_usd = 0.0
        trade_funding_rows = (funding_events or {}).get(symbol, ())
        trade_funding_prices = funding_price_by_symbol.get(symbol, [])
        notional_for_funding = notional

        for future in bars[start_index:]:
            if future.open_time < entry_time or future.open_time > now_epoch:
                continue
            # Excursions are measured from the actual entry fill and use the trade candle OHLC.
            if direction == "LONG":
                mae_usd = max(mae_usd, max(0.0, (entry_fill - future.low) * quantity_units))
                mfe_usd = max(mfe_usd, max(0.0, (future.high - entry_fill) * quantity_units))
            else:
                mae_usd = max(mae_usd, max(0.0, (future.high - entry_fill) * quantity_units))
                mfe_usd = max(mfe_usd, max(0.0, (entry_fill - future.low) * quantity_units))

            funding_to_bar = _funding_events_for_trade(
                trade_funding_rows, symbol, entry_time, future.close_time,
                notional_for_funding, cfg.assumed_funding_rate_per_8h, direction,
                quantity_units=quantity_units, fair_price_bars=trade_funding_prices,
            )
            wallet_at_bar = wallet_after_entry_fee - funding_to_bar
            current_liquidation_price = _cross_liquidation_price(
                direction=direction, entry_price=entry_fill, unit_quantity=quantity_units,
                wallet_balance=wallet_at_bar, maintenance_rate=maintenance_rate,
                liquidation_fee_rate=liquidation_fee_rate, isolated_margin=isolated_margin,
            )

            fair_bar = fair_map.get(future.open_time)
            if fair_bar is not None and current_liquidation_price is not None:
                if direction == "LONG" and fair_bar.low <= current_liquidation_price:
                    exit_reason = "LIQUIDATION"
                    exit_reference = fair_bar.open if fair_bar.open <= current_liquidation_price else current_liquidation_price
                elif direction == "SHORT" and fair_bar.high >= current_liquidation_price:
                    exit_reason = "LIQUIDATION"
                    exit_reference = fair_bar.open if fair_bar.open >= current_liquidation_price else current_liquidation_price
            if exit_reason:
                exit_time, exit_bar = future.close_time, future
                break

            # Stop gaps receive the candle open (worse execution); otherwise SL has precedence
            # over TP when OHLC cannot reveal intrabar ordering.
            if direction == "LONG":
                if future.open <= stop_price:
                    exit_reason, exit_reference = "STOP_LOSS_GAP", future.open
                elif future.low <= stop_price:
                    exit_reason, exit_reference = "STOP_LOSS", stop_price
                elif future.high >= target_price:
                    exit_reason, exit_reference = "TAKE_PROFIT", target_price
            else:
                if future.open >= stop_price:
                    exit_reason, exit_reference = "STOP_LOSS_GAP", future.open
                elif future.high >= stop_price:
                    exit_reason, exit_reference = "STOP_LOSS", stop_price
                elif future.low <= target_price:
                    exit_reason, exit_reference = "TAKE_PROFIT", target_price
            if exit_reason:
                exit_time, exit_bar = future.close_time, future
                break

            if future.close_time - entry_time >= cfg.max_holding_minutes * 60:
                exit_reason, exit_reference = "EXPIRED", future.close
                exit_time, exit_bar = future.close_time, future
                break

        # Define 1R as the full modeled net loss if the planned stop executes,
        # including entry/exit taker fees and both-side slippage.
        risk_amount = expected_stop_loss
        if risk_amount <= 0:
            # The trade is not mathematically risk-sized if costs make the modeled
            # stop outcome non-negative; refuse to report misleading R statistics.
            continue
        if exit_reason is None:
            # No TP/SL/expiry/liquidation: keep this as open, mark-to-market, and do not
            # falsely classify it as a resolved trade at the end of the historical window.
            last_bar = next((bar for bar in reversed(bars[start_index:]) if bar.close_time <= now_epoch), None)
            if last_bar is None:
                blocked_due_open_position += 1
                continue
            exit_bar = last_bar
            exit_time = last_bar.close_time
            exit_reference = last_bar.close
            mark = _net_exit_pnl(
                direction=direction, ref_entry=reference_entry, entry_fill=entry_fill,
                exit_ref=exit_reference, quantity_units=quantity_units, fee_rate=contract_fee_rate,
                slippage_rate=slip,
                funding_cost=_funding_events_for_trade(
                    trade_funding_rows, symbol, entry_time, exit_time, notional_for_funding,
                    cfg.assumed_funding_rate_per_8h, direction,
                    quantity_units=quantity_units, fair_price_bars=trade_funding_prices,
                ),
            )
            unrealised_total += mark["net_pnl"]
            unresolved_count += 1
            ledger.append({
                "symbol": symbol, "direction": direction, "signal_time": signal_bar.close_time,
                "entry_time": entry_time, "exit_time": None, "signal_reason": ";".join(context["signal"]["reason"]),
                "signal_close": signal_bar.close, "entry_reference_price": reference_entry,
                "entry_fill_price": entry_fill, "exit_reference_price": None, "exit_fill_price": None,
                "mark_price": exit_reference, "quantity": quantity, "contract_size": contract_size,
                "leverage": leverage, "margin_mode": cfg.margin_mode, "initial_margin": actual_initial_margin,
                "risk_tier": tier_terms.get("tier"), "risk_tier_status": tier_terms.get("status"),
                "maintenance_margin_rate": maintenance_rate,
                "price_tick": price_tick, "taker_fee_rate": contract_fee_rate,
                "planned_net_stop_loss": expected_stop_loss,
                "planned_net_take_profit": expected_target_profit,
                "cross_wallet_balance_at_entry": wallet_after_entry_fee, "notional": notional,
                "stop_price": stop_price, "target_price": target_price, "liquidation_price_estimate": liquidation_price,
                "rsi": context["signal"]["rsi"], "ema21": context["signal"]["ema21"],
                "ema50": context["signal"]["ema50"], "ema100": context["signal"]["ema100"],
                "gross_pnl": mark["gross_pnl"], "entry_fee": mark["entry_fee"],
                "exit_fee_estimate": mark["exit_fee"], "fees": mark["fees"],
                "funding_cost": mark["funding_cost"], "estimated_slippage_cost": mark["slippage_cost"],
                "liquidation_fee": 0.0, "net_pnl": mark["net_pnl"], "realized_pnl": None,
                "realized_r": None, "mae_r": mae_usd / risk_amount, "mfe_r": mfe_usd / risk_amount,
                "signal_rr": signal_rr, "actual_fill_rr": actual_fill_rr,
                "exit_reason": "OPEN_UNRESOLVED", "holding_minutes": max(0.0, (exit_time - entry_time) / 60.0),
                "equity_after": equity + mark["net_pnl"], "entry_mode": "MARKET",
            })
            equity_curve.append({"timestamp": exit_time, "equity": equity + mark["net_pnl"]})
            active_until = now_epoch
            break

        funding_cost = _funding_events_for_trade(
            trade_funding_rows, symbol, entry_time, exit_time, notional_for_funding,
            cfg.assumed_funding_rate_per_8h, direction,
            quantity_units=quantity_units, fair_price_bars=trade_funding_prices,
        )
        liquidation_fee_rate_used = liquidation_fee_rate if exit_reason == "LIQUIDATION" else 0.0
        costs = _net_exit_pnl(
            direction=direction, ref_entry=reference_entry, entry_fill=entry_fill,
            exit_ref=exit_reference, quantity_units=quantity_units, fee_rate=contract_fee_rate,
            slippage_rate=slip, funding_cost=funding_cost,
            liquidation_fee_rate=liquidation_fee_rate_used,
        )
        net_pnl = costs["net_pnl"]
        equity = max(0.0, equity + net_pnl)
        peak_equity = max(peak_equity, equity)
        drawdown_pct = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 100.0
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
        gross_total += costs["gross_pnl"]
        fees_total += costs["fees"]
        slippage_total += costs["slippage_cost"]
        funding_total += funding_cost
        liquidation_fees_total += costs["liquidation_fee"]
        peak_margin_used = max(peak_margin_used, actual_initial_margin)
        realized_r = net_pnl / risk_amount if risk_amount > 0 else 0.0
        ledger.append({
            "symbol": symbol, "direction": direction, "signal_time": signal_bar.close_time,
            "entry_time": entry_time, "exit_time": exit_time, "signal_reason": ";".join(context["signal"]["reason"]),
            "signal_close": signal_bar.close, "entry_reference_price": reference_entry,
            "entry_fill_price": entry_fill, "exit_reference_price": exit_reference,
            "exit_fill_price": costs["exit_fill"], "quantity": quantity,
            "contract_size": contract_size, "leverage": leverage, "margin_mode": cfg.margin_mode,
            "risk_tier": tier_terms.get("tier"), "risk_tier_status": tier_terms.get("status"),
            "maintenance_margin_rate": maintenance_rate,
            "price_tick": price_tick, "taker_fee_rate": contract_fee_rate,
            "planned_net_stop_loss": expected_stop_loss,
            "planned_net_take_profit": expected_target_profit,
            "initial_margin": actual_initial_margin, "cross_wallet_balance_at_entry": wallet_after_entry_fee,
            "notional": notional, "stop_price": stop_price, "target_price": target_price,
            "liquidation_price_estimate": liquidation_price, "rsi": context["signal"]["rsi"],
            "ema21": context["signal"]["ema21"], "ema50": context["signal"]["ema50"],
            "ema100": context["signal"]["ema100"], "gross_pnl": costs["gross_pnl"],
            "entry_fee": costs["entry_fee"], "exit_fee": costs["exit_fee"], "fees": costs["fees"],
            "funding_cost": funding_cost, "estimated_slippage_cost": costs["slippage_cost"],
            "liquidation_fee": costs["liquidation_fee"], "net_pnl": net_pnl, "realized_pnl": net_pnl,
            "realized_r": realized_r, "mae_r": mae_usd / risk_amount, "mfe_r": mfe_usd / risk_amount,
            "signal_rr": signal_rr, "actual_fill_rr": actual_fill_rr,
            "exit_reason": exit_reason, "holding_minutes": max(0.0, (exit_time - entry_time) / 60.0),
            "equity_after": equity, "drawdown_pct": drawdown_pct, "entry_mode": "MARKET",
        })
        liquidations += int(exit_reason == "LIQUIDATION")
        expiries += int(exit_reason == "EXPIRED")
        equity_curve.append({"timestamp": exit_time, "equity": equity})
        active_until = exit_time

    resolved = [trade for trade in ledger if trade["exit_reason"] != "OPEN_UNRESOLVED"]
    winners = [trade for trade in resolved if float(trade["net_pnl"]) > 1e-12]
    losers = [trade for trade in resolved if float(trade["net_pnl"]) < -1e-12]
    gross_wins = sum(float(trade["net_pnl"]) for trade in winners)
    gross_losses = abs(sum(float(trade["net_pnl"]) for trade in losers))
    realized_rs = [float(trade["realized_r"]) for trade in resolved]
    cumulative_r = 0.0
    peak_r = 0.0
    max_drawdown_r = 0.0
    for realized_r in realized_rs:
        cumulative_r += realized_r
        peak_r = max(peak_r, cumulative_r)
        max_drawdown_r = max(max_drawdown_r, peak_r - cumulative_r)

    tps = [trade for trade in resolved if trade["exit_reason"] == "TAKE_PROFIT"]
    sls = [trade for trade in resolved if str(trade["exit_reason"]).startswith("STOP_LOSS")]
    signal_rr_values = [float(trade["signal_rr"]) for trade in ledger]
    actual_rr_values = [float(trade["actual_fill_rr"]) for trade in ledger]
    mae_values = [float(trade["mae_r"]) for trade in ledger]
    mfe_values = [float(trade["mfe_r"]) for trade in ledger]
    total_r = sum(realized_rs)
    avg_realized_r = total_r / len(resolved) if resolved else None
    ending_equity = equity + unrealised_total
    data_available = any(eligible.values())
    traded_symbols = {str(trade["symbol"]) for trade in ledger}
    missing_funding = sorted(
        symbol for symbol in traded_symbols
        if funding_events is None or not (funding_events or {}).get(symbol)
    )
    missing_fair_price = sorted(symbol for symbol in traded_symbols if symbol not in fair_by_symbol)

    report = {
        "status": "COMPLETED_WITH_DATA" if data_available else "NOT_RUN_NO_VERIFIED_BARS",
        "strategy": "RSI_EMA_SYMMETRIC_MOMENTUM_3M",
        "window": f"{window_days}D", "window_days": window_days,
        "data_start": cutoff, "data_end": now_epoch,
        "starting_equity": cfg.starting_equity, "ending_equity": ending_equity,
        "net_profit": ending_equity - cfg.starting_equity,
        "realized_net_profit": equity - cfg.starting_equity,
        "unrealized_pnl_at_end": unrealised_total,
        "net_return_pct": (ending_equity - cfg.starting_equity) / cfg.starting_equity * 100,
        "coins_tested": len(candles_by_symbol), "coins_with_data": len(eligible),
        "signals": signal_count, "long_signals": long_signals, "short_signals": short_signals,
        # Compatibility aliases for earlier research dashboards.
        "long_count": long_signals, "short_count": short_signals,
        "signals_with_contract_metadata": len(candidates),
        "tradeable_signals": len(candidates),
        "blocked_signals_due_to_open_position": blocked_due_open_position,
        "unfilled_signals": max(0, signal_count - len(candidates)),
        "resolved": len(resolved), "open_unresolved": unresolved_count,
        "completed_trades": len(resolved), "trades_opened": len(ledger),
        "wins": len(winners), "losses": len(losers),
        "win_rate_pct": len(winners) / len(resolved) * 100 if resolved else None,
        "take_profit_exits": len(tps),
        "long_take_profits": sum(trade["direction"] == "LONG" for trade in tps),
        "short_take_profits": sum(trade["direction"] == "SHORT" for trade in tps),
        "stop_loss_exits": len(sls),
        "long_stop_losses": sum(trade["direction"] == "LONG" for trade in sls),
        "short_stop_losses": sum(trade["direction"] == "SHORT" for trade in sls),
        "expired_exits": expiries, "liquidation_exits": liquidations,
        "end_of_window_unresolved": unresolved_count,
        "avg_signal_rr": sum(signal_rr_values) / len(signal_rr_values) if signal_rr_values else None,
        "avg_actual_fill_rr": sum(actual_rr_values) / len(actual_rr_values) if actual_rr_values else None,
        "avg_planned_net_take_profit": (
            sum(float(trade["planned_net_take_profit"]) for trade in ledger) / len(ledger) if ledger else None
        ),
        "avg_planned_net_stop_loss": (
            sum(float(trade["planned_net_stop_loss"]) for trade in ledger) / len(ledger) if ledger else None
        ),
        "avg_realized_r": avg_realized_r, "total_r": total_r,
        "expectancy_r_per_trade": avg_realized_r,
        "profit_factor": gross_wins / gross_losses if gross_losses > 0 else (None if not winners else "unbounded_no_losses"),
        "max_drawdown_r": max_drawdown_r, "max_losing_streak": _max_losing_streak(resolved),
        "avg_mae_r": sum(mae_values) / len(mae_values) if mae_values else None,
        "avg_mfe_r": sum(mfe_values) / len(mfe_values) if mfe_values else None,
        "market_entries": sum(trade.get("entry_mode") == "MARKET" for trade in ledger),
        "limit_entries": sum(trade.get("entry_mode") == "LIMIT" for trade in ledger),
        "gross_pnl": gross_total, "trading_fees": fees_total, "funding_costs": funding_total,
        "estimated_slippage_cost": slippage_total, "liquidation_fees": liquidation_fees_total,
        "max_drawdown_pct": max_drawdown_pct, "peak_margin_used": peak_margin_used,
        "margin_mode": cfg.margin_mode, "max_concurrent_positions": 1 if ledger else 0,
        "average_holding_minutes": (
            sum(float(trade["holding_minutes"]) for trade in resolved) / len(resolved) if resolved else None
        ),
        "per_symbol": _per_symbol(ledger), "trade_ledger": ledger, "equity_curve": equity_curve,
        "data_quality": {
            "symbols_with_bars": sorted(eligible),
            "bar_counts": {symbol: len(bars) for symbol, bars in eligible.items()},
            "warmup_bars_used": {symbol: max(0, len(warmup[symbol]) - len(eligible[symbol])) for symbol in eligible},
            "historical_funding_events_supplied": funding_events is not None,
            "funding_missing_symbols": missing_funding,
            "fair_price_symbols": sorted(fair_data_symbols),
            "fair_price_missing_symbols": missing_fair_price,
            "taker_fee_fallback_symbols": sorted(fee_fallback_symbols),
            "nonstandard_fee_mode_symbols": sorted(nonstandard_fee_mode_symbols),
            "liquidation_fee_fallback_symbols": sorted(liquidation_fee_fallback_symbols),
            "risk_tier_unknown_symbols": sorted(risk_tier_unknown_symbols),
            "risk_tier_rejected_signals": risk_tier_rejected_signals,
            "risk_tiers_used": {symbol: sorted(levels) for symbol, levels in sorted(risk_tiers_used.items())},
            "funding_valuation_source": "latest completed MEXC fair-price candle at or before settlement; entry-notional fallback if unavailable",
            "liquidation_trigger_source": "MEXC fair-price OHLC when supplied; no liquidation inference for symbols without it",
            "limitations": [
                "Synthetic market fills; no exchange order-book replay or confirmed fills.",
                "Cross-margin equation assumes this backtest holds at most one bot position at a time, has no other account positions and has no open order margins or isolated positions.",
                "MEXC liquidation is triggered by Fair Price; liquidation checks are disabled where historical fair-price bars are unavailable.",
                "Risk tiers use published INCREASE/CUSTOM metadata when complete; symbols without tier metadata fall back to base MMR and are flagged.",
                "Missing contract taker/liquidation fee fields use configured fallbacks; dynamic/tiered fee modes may differ from account-specific fees.",
                "Intrabar stop/target order is unknown from OHLC; stop loss is prioritized over take profit.",
                "Positive funding rates are charged to LONG and credited to SHORT; missing settlement history is excluded and flagged.",
            ],
        },
        "assumptions": {
            **asdict(cfg), "strategy": asdict(cfg.strategy),
            "taker_fee_rate_per_side": cfg.taker_fee_rate,
            "slippage_bps_per_side": cfg.slippage_bps_per_side,
            "margin_model": "MEXC published cross-margin single-position equation" if cfg.margin_mode == "cross" else "isolated position margin equation",
            "wallet_balance_includes_shared_cross_collateral": cfg.margin_mode == "cross",
            "max_concurrent_positions": cfg.max_positions,
            "one_r_definition": "net planned stop loss including contract taker fees, entry/exit slippage and price-tick rounding",
            "requested_stop_loss_is_net_usdt": True,
            "actual_live_orders": False,
        },
    }
    return report


def _per_symbol(ledger: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for trade in ledger:
        symbol = str(trade["symbol"])
        row = result.setdefault(symbol, {
            "trades": 0, "wins": 0, "net_pnl": 0.0, "fees": 0.0,
            "gross_pnl": 0.0, "liquidations": 0, "long": 0, "short": 0,
        })
        row["trades"] += 1
        row["wins"] += int(trade.get("realized_pnl") is not None and float(trade["realized_pnl"]) > 0)
        row["net_pnl"] += float(trade.get("net_pnl", 0.0))
        row["fees"] += float(trade.get("fees", 0.0))
        row["gross_pnl"] += float(trade.get("gross_pnl", 0.0))
        row["liquidations"] += int(trade.get("exit_reason") == "LIQUIDATION")
        row[str(trade.get("direction", "LONG")).lower()] += 1
    return result


def format_backtest_whatsapp(report: Mapping[str, Any], *, elapsed_seconds: float | None = None) -> str:
    """Render the detailed concise report requested for WhatsApp."""
    if report.get("status") == "NOT_RUN_NO_VERIFIED_BARS":
        return f"💥BACKTEST {report.get('window', 'N/A')} 💥\n❌ No verified completed 3M candles available. No performance result calculated."

    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.1f}%"

    def ratio(value: Any) -> str:
        if value is None:
            return "n/a"
        if value == "unbounded_no_losses":
            return "∞ (no losing trades)"
        return f"{float(value):.2f}"

    def r_value(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):+.2f}R"

    def r_plain(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.2f}R"

    lines = [
        f"💥BACKTEST {report['window']} 💥",
        "",
        f"🪙 Coins Tested: {report['coins_tested']}",
        f"📡 Signals: {report['signals']}",
        f"🟢 LONG: {report['long_signals']}",
        f"🔴 SHORT: {report['short_signals']}",
        f"✅ Resolved: {report['resolved']}",
        f"⏳ Open/Unresolved: {report['open_unresolved']}",
        f"🎯 Total TP: {report['take_profit_exits']}",
        f"   🟢 LONG TPs: {report['long_take_profits']}",
        f"   🔴 SHORT TPs: {report['short_take_profits']}",
        f"🛑 Total SL: {report['stop_loss_exits']}",
        f"   🟢 LONG Trade SLs: {report['long_stop_losses']}",
        f"   🔴 SHORT Trade SLs: {report['short_stop_losses']}",
        f"⌛ Expired: {report['expired_exits']}",
        f"⚠️ Liquidations: {report['liquidation_exits']}",
        "",
        f"📈 Win Rate: {pct(report['win_rate_pct'])}",
        f"⚖️ Avg Signal RR: {ratio(report['avg_signal_rr'])}",
        f"🎯 Avg Actual Fill RR: {ratio(report['avg_actual_fill_rr'])}",
        f"💰 Avg Realized R: {r_value(report['avg_realized_r'])}",
        f"💰 Total R: {r_value(report['total_r'])}",
        f"📊 Expectancy: {r_value(report['expectancy_r_per_trade'])}/trade" if report['expectancy_r_per_trade'] is not None else "📊 Expectancy: n/a",
        f"📐 Profit Factor: {ratio(report['profit_factor'])}",
        f"📉 Max Drawdown: {r_plain(report['max_drawdown_r'])}",
        f"📉 Max Losing Streak: {report['max_losing_streak']}",
        f"🧭 Avg MAE / MFE: {r_plain(report['avg_mae_r'])} / {r_plain(report['avg_mfe_r'])}",
        f"⚙️ Entry Mode: MARKET {report['market_entries']} / LIMIT {report['limit_entries']}",
        f"📥 Signals not entered: {report['unfilled_signals']} · Blocked by open position: {report['blocked_signals_due_to_open_position']}",
        "",
        f"💳 Wallet: ${report['starting_equity']:.2f} → ${report['ending_equity']:.2f}",
        f"💵 Net P&L: ${report['net_profit']:+.2f} (realized ${report['realized_net_profit']:+.2f})",
        f"💸 Fees: ${report['trading_fees']:.4f} · Funding: ${report['funding_costs']:+.4f} · Slippage: ${report['estimated_slippage_cost']:.4f}",
        f"🧱 Margin: {str(report['margin_mode']).upper()} · Initial margin/trade ${float(report['assumptions']['initial_margin']):.2f} · Max positions {report['max_concurrent_positions']}",
        f"🛡️ Avg planned net TP/SL: +${float(report['avg_planned_net_take_profit']):.2f} / -${float(report['avg_planned_net_stop_loss']):.2f}"
        if report.get("avg_planned_net_take_profit") is not None and report.get("avg_planned_net_stop_loss") is not None
        else "🛡️ Avg planned net TP/SL: n/a",
    ]
    warnings = []
    missing_funding = report.get("data_quality", {}).get("funding_missing_symbols", [])
    missing_fair = report.get("data_quality", {}).get("fair_price_missing_symbols", [])
    partial_funding = report.get("data_quality", {}).get("funding_partial_coverage_symbols", [])
    if missing_funding:
        warnings.append(f"Funding history missing for {len(missing_funding)} symbol(s); P&L excludes unknown funding.")
    nonstandard_fees = report.get("data_quality", {}).get("nonstandard_fee_mode_symbols", [])
    if nonstandard_fees:
        warnings.append(f"Dynamic/tiered exchange fee mode applies to {len(nonstandard_fees)} symbol(s); account-specific fees may differ from contract base rates.")
    if partial_funding:
        warnings.append(f"Funding history only partially covers {len(partial_funding)} symbol(s); some historic funding may be missing.")
    unknown_tiers = report.get("data_quality", {}).get("risk_tier_unknown_symbols", [])
    if unknown_tiers:
        warnings.append(f"Risk-tier metadata unavailable for {len(unknown_tiers)} traded symbol(s); base MMR fallback used.")
    if report.get("data_quality", {}).get("risk_tier_rejected_signals"):
        warnings.append(f"{report['data_quality']['risk_tier_rejected_signals']} signal(s) skipped: position exceeded published MEXC risk-tier limits.")
    if missing_fair:
        warnings.append(f"Fair-price history missing for {len(missing_fair)} symbol(s); liquidation checks were unavailable for those markets.")
    if report.get("open_unresolved"):
        warnings.append("Open trades are marked to market and are not included in realized R/win-rate statistics.")
    warnings.append("Research simulation only: synthetic market fills; order-book impact and positions outside this bot are not replayed.")
    warnings.append("Cross-margin liquidation is estimated for this bot's single active position; account-wide concurrent positions and open-order margin are not replayed.")
    lines.extend(["", *["⚠️ " + warning for warning in warnings]])
    if elapsed_seconds is not None:
        lines.append(f"Top turnover universe · {elapsed_seconds:.1f}s · Live orders OFF")
    return "\n".join(lines)


def write_backtest_artifacts(report: Mapping[str, Any], output_dir: str | Path, *, prefix: str) -> dict[str, str]:
    """Write JSON, CSV ledger and human-readable TXT report."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{prefix}.json"
    csv_path = out / f"{prefix}_trades.csv"
    txt_path = out / f"{prefix}.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False, default=str), encoding="utf-8")
    ledger = list(report.get("trade_ledger", []))
    columns = list(dict.fromkeys(key for row in ledger for key in row)) if ledger else [
        "symbol", "direction", "signal_time", "entry_time", "exit_time", "net_pnl", "exit_reason",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ledger)
    txt_path.write_text(format_backtest_whatsapp(report) + "\n", encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "text": str(txt_path)}


def render_equity_curve(report: Mapping[str, Any], output_path: str | Path) -> Path:
    """Render simulated equity points; unresolved positions are shown at their end mark."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from datetime import datetime, timezone

    points = list(report.get("equity_curve", []))
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
        ax.text(0.5, 0.5, "No verified candles", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(f"RSI/EMA Cross-Margin Backtest — {report.get('window', '')} (simulated)")
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
