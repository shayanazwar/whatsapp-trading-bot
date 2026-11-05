"""Event-driven backtesting for fixed-payout scenarios and modeled exchange-realistic P&L.

The fixed-payout mode is intentionally hypothetical; the exchange-realistic mode is only as
reliable as its data, fee, funding, contract multiplier and slippage assumptions.
"""
from __future__ import annotations

import math
from bisect import bisect_right
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.market.candles import Candle, aggregate_12h_from_4h
from app.market.indicators import atr
from app.risk import contract_risk_tier_terms
from app.strategy.rfpc import TradeCandidate, evaluate_strategy

STANDARD_WINDOWS = {"1D": 1, "3D": 3, "7D": 7, "30D": 30, "60D": 60, "90D": 90, "180D": 180, "365D": 365}


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    mode: str = "fixed_payout"
    starting_balance: float = 100.0
    tp_pct: float = 5.0
    sl_pct: float = 2.0
    fixed_win_payout: float = 20.0
    fixed_loss_payout: float = 5.0
    risk_fraction: float = 0.005
    taker_fee_rate: float = 0.0008
    slippage_bps_per_side: float = 5.0
    cost_multiplier: float = 1.0
    max_trades_per_symbol: int = 10000
    max_holding_hours: float = 240.0
    reward_risk_multiple: float = 2.0
    max_concurrent_positions: int = 50
    max_open_risk_fraction: float = 0.25
    margin_mode: str = "cross"
    initial_margin_per_position: float = 1.0
    max_leverage: int = 50
    max_total_notional_multiple: float = 25.0
    liquidation_fee_rate_fallback: float = 0.0

    def __post_init__(self) -> None:
        if self.mode not in {"fixed_payout", "exchange_realistic"}:
            raise ValueError("mode must be fixed_payout or exchange_realistic")
        if self.starting_balance <= 0 or self.tp_pct <= 0 or self.sl_pct <= 0:
            raise ValueError("Starting balance and TP/SL percentages must be positive")
        if self.fixed_win_payout < 0 or self.fixed_loss_payout < 0:
            raise ValueError("Fixed payouts cannot be negative")
        if not 0 < self.risk_fraction <= 0.05:
            raise ValueError("risk_fraction must be in (0, 0.05]")
        if self.taker_fee_rate < 0 or self.slippage_bps_per_side < 0 or self.cost_multiplier < 1:
            raise ValueError("Invalid cost inputs")
        if not math.isfinite(float(self.max_holding_hours)) or self.max_holding_hours <= 0:
            raise ValueError("max_holding_hours must be finite and positive")
        if not 1 <= self.max_concurrent_positions <= 50:
            raise ValueError("max_concurrent_positions must be between 1 and 50")
        if not math.isfinite(self.max_open_risk_fraction) or not 0 < self.max_open_risk_fraction <= 0.50:
            raise ValueError("max_open_risk_fraction must be in (0, 0.50]")
        if self.margin_mode != "cross":
            raise ValueError("The active S1-RFPC research engine supports CROSS margin only")
        if not math.isfinite(self.initial_margin_per_position) or self.initial_margin_per_position <= 0:
            raise ValueError("initial_margin_per_position must be finite and positive")
        if not 1 <= int(self.max_leverage) <= 50:
            raise ValueError("max_leverage must be between 1 and 50")
        if not math.isfinite(self.max_total_notional_multiple) or self.max_total_notional_multiple < 1:
            raise ValueError("max_total_notional_multiple must be finite and at least 1")
        if not math.isfinite(self.liquidation_fee_rate_fallback) or not 0 <= self.liquidation_fee_rate_fallback < 0.1:
            raise ValueError("liquidation_fee_rate_fallback must be in [0, 0.1)")
        if not math.isfinite(self.reward_risk_multiple) or not 0.25 <= self.reward_risk_multiple <= 10:
            raise ValueError("reward_risk_multiple must be finite and between 0.25R and 10R")


@dataclass(slots=True)
class SimPosition:
    symbol: str
    side: str
    entry_index: int
    entry_time: int
    entry_price: float
    stop_price: float
    target_price: float
    candidate_id: str
    setup_key: str
    risk_amount: float
    quantity: float = 0.0
    contract_size: float = 1.0
    opening_fee: float = 0.0
    funding_pnl: float = 0.0
    mode: str = "fixed_payout"
    funding_checked_until: int = 0
    initial_margin: float = 0.0
    leverage: float = 0.0
    maintenance_margin_rate: float = 0.0
    liquidation_fee_rate: float = 0.0
    estimated_close_fee: float = 0.0
    estimated_close_slippage: float = 0.0
    risk_tier_status: str = "BASE_MMR_FALLBACK"
    risk_tier: int | None = None
    signal_rr: float = 0.0
    actual_fill_rr: float = 0.0
    final_mark_price: float | None = None
    mae_r: float = 0.0
    mfe_r: float = 0.0


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    symbol: str
    side: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl: float
    realised_r: float
    risk_amount: float
    candidate_id: str
    mode: str
    fees: float = 0.0
    funding_pnl: float = 0.0
    slippage_cost: float = 0.0
    liquidation_fee: float = 0.0
    signal_rr: float = 0.0
    actual_fill_rr: float = 0.0
    mae_r: float = 0.0
    mfe_r: float = 0.0
    holding_hours: float = 0.0
    quantity: float = 0.0
    contract_size: float = 1.0
    initial_margin: float = 0.0
    leverage: float = 0.0
    maintenance_margin_rate: float = 0.0
    liquidation_fee_rate: float = 0.0
    risk_tier_status: str = "NOT_APPLICABLE"
    risk_tier: int | None = None
    margin_mode: str = "N/A"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _floor_step(value: float, step: float) -> float:
    if step <= 0:
        return 0.0
    # Add a tiny tolerance only to compensate floating-point representation at exact steps.
    return max(0.0, math.floor((value + step * 1e-10) / step) * step)


def _accrue_funding_through(
    position: SimPosition,
    candle: Candle,
    through_time: int,
    funding_events: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    *,
    allow_favorable: bool = True,
) -> None:
    """Accrue known historical settlements as the event simulation advances.

    The settlement notional is approximated using the 1H candle open when settlement
    is at/before that open, or the 1H close when settlement falls inside the candle.
    If a stop/target exits inside the same 1H candle, favorable funding within that
    candle is not credited because the event sequence is unknowable; adverse funding
    is still charged conservatively.
    """
    if position.mode != "exchange_realistic":
        return
    previous = max(position.entry_time, position.funding_checked_until or position.entry_time)
    if through_time <= previous:
        return
    rows = (funding_events or {}).get(position.symbol, ())
    side_sign = 1.0 if position.side == "LONG" else -1.0
    for row in rows:
        try:
            settle = int(float(row.get("settleTime", row.get("settle_time", row.get("fundingTime", 0)))))
            if settle > 100_000_000_000:
                settle //= 1000
            rate = float(row.get("fundingRate", row.get("funding_rate", 0.0)))
        except (TypeError, ValueError, OverflowError):
            continue
        if not previous < settle <= through_time or settle <= position.entry_time:
            continue
        contribution_is_inside_candle = settle > candle.open_time
        price = candle.close if contribution_is_inside_candle else candle.open
        notional = position.quantity * position.contract_size * price
        contribution = -side_sign * rate * notional
        if contribution_is_inside_candle and not allow_favorable and contribution > 0:
            continue
        position.funding_pnl += contribution
    position.funding_checked_until = through_time

def _entry_price(raw_open: float, side: str, slippage_rate: float) -> float:
    return raw_open * (1 + slippage_rate if side == "LONG" else 1 - slippage_rate)


def _exit_price(raw_price: float, side: str, slippage_rate: float) -> float:
    # Selling to exit long is adversely lower; buying to exit short is adversely higher.
    return raw_price * (1 - slippage_rate if side == "LONG" else 1 + slippage_rate)


def _outcome_on_bar(position: SimPosition, candle: Candle) -> tuple[float, str] | None:
    """Return raw exit price/reason. Conservative ordering when stop and target both touch."""
    long_side = position.side == "LONG"
    if long_side:
        if candle.open <= position.stop_price:
            return candle.open, "STOP_GAP"
        if candle.open >= position.target_price:
            return candle.open, "TARGET_GAP"
        hit_stop = candle.low <= position.stop_price
        hit_target = candle.high >= position.target_price
    else:
        if candle.open >= position.stop_price:
            return candle.open, "STOP_GAP"
        if candle.open <= position.target_price:
            return candle.open, "TARGET_GAP"
        hit_stop = candle.high >= position.stop_price
        hit_target = candle.low <= position.target_price
    if hit_stop:
        return position.stop_price, "STOP" if not hit_target else "STOP_AMBIGUOUS_BAR"
    if hit_target:
        return position.target_price, "TARGET"
    return None


def _update_excursion_at_price(position: SimPosition, price: float) -> None:
    """Update MAE/MFE from one price known to be reachable before the position exits."""
    if position.risk_amount <= 0 or not math.isfinite(float(price)) or price <= 0:
        return
    direction = 1.0 if position.side == "LONG" else -1.0
    signed_move = (float(price) - position.entry_price) * direction
    move_r = signed_move * position.quantity * position.contract_size / position.risk_amount
    if move_r < 0:
        position.mae_r = max(position.mae_r, -move_r)
    elif move_r > 0:
        position.mfe_r = max(position.mfe_r, move_r)


def _update_excursions_from_open_bar(position: SimPosition, candle: Candle) -> None:
    """Use full OHLC extremes only when the position survived the entire candle."""
    adverse = candle.low if position.side == "LONG" else candle.high
    favorable = candle.high if position.side == "LONG" else candle.low
    _update_excursion_at_price(position, adverse)
    _update_excursion_at_price(position, favorable)


def _drawdown(equity_points: Sequence[float]) -> tuple[float, float]:
    if not equity_points:
        return 0.0, 0.0
    peak = equity_points[0]
    max_abs = 0.0
    max_pct = 0.0
    for value in equity_points:
        peak = max(peak, value)
        dd = max(0.0, peak - value)
        pct = dd / peak if peak > 0 else 0.0
        max_abs = max(max_abs, dd)
        max_pct = max(max_pct, pct)
    return max_abs, max_pct * 100.0


def _metrics(
    *,
    window: str,
    mode: str,
    start_time: int,
    end_time: int,
    starting_balance: float,
    ending_balance: float,
    trades: Sequence[ClosedTrade],
    open_positions: Sequence[SimPosition],
    equity_curve: Sequence[float],
    symbols_tested: int,
    symbols_eligible: int,
    valid_candles: int,
    rejection_counts: Mapping[str, int],
    warnings: Sequence[str],
    max_observed_concurrent_positions: int = 0,
    configured_max_concurrent_positions: int = 50,
    positions_entered: int | None = None,
    candidate_signals: int = 0,
    long_signals: int = 0,
    short_signals: int = 0,
    final_unrealized_pnl: float = 0.0,
    max_initial_margin_used: float = 0.0,
    max_notional_used: float = 0.0,
    liquidation_events: int = 0,
    unverified_liquidation_fee_positions: int = 0,
    cross_margin_modelled: bool = False,
    liquidation_price_basis: str = "NOT_MODELLED",
) -> dict[str, Any]:
    wins = [t for t in trades if t.pnl > 1e-12]
    losses = [t for t in trades if t.pnl < -1e-12]
    breakeven = [t for t in trades if abs(t.pnl) <= 1e-12]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    pnl_sum = sum(t.pnl for t in trades)
    count = len(trades)
    win_rate = (len(wins) / count * 100) if count else None
    expectancy = pnl_sum / count if count else None
    r_values = [t.realised_r for t in trades]
    expectancy_r = statistics.fmean(r_values) if r_values else None
    signal_rr_values = [t.signal_rr for t in trades] + [p.signal_rr for p in open_positions]
    fill_rr_values = [t.actual_fill_rr for t in trades] + [p.actual_fill_rr for p in open_positions]
    mae_values = [t.mae_r for t in trades] + [p.mae_r for p in open_positions]
    mfe_values = [t.mfe_r for t in trades] + [p.mfe_r for p in open_positions]
    max_dd, max_dd_pct = _drawdown(equity_curve)
    losing_streak = current_streak = 0
    for trade in trades:
        if trade.pnl < -1e-12:
            current_streak += 1
            losing_streak = max(losing_streak, current_streak)
        else:
            current_streak = 0
    entered = int(positions_entered if positions_entered is not None else count + len(open_positions))
    not_entered = max(0, int(candidate_signals) - entered)
    ending_equity = ending_balance + final_unrealized_pnl
    ledger_pnl = sum(t.pnl for t in trades)
    wallet_error = (ending_balance - starting_balance) - ledger_pnl

    # Independently reconstruct the open-position mark-to-market ledger. This avoids the
    # previous tautological check that only compared ending_equity to the expression used
    # to create ending_equity. A missing final mark is a failed reconciliation.
    reconstructed_open_unrealized = 0.0
    missing_final_marks = 0
    if mode == "exchange_realistic":
        for position in open_positions:
            mark = position.final_mark_price
            if mark is None or not math.isfinite(float(mark)) or float(mark) <= 0:
                missing_final_marks += 1
                continue
            direction = 1.0 if position.side == "LONG" else -1.0
            gross = position.quantity * position.contract_size * (float(mark) - position.entry_price) * direction
            reconstructed_open_unrealized += (
                gross - position.opening_fee - position.estimated_close_fee
                - position.estimated_close_slippage + position.funding_pnl
            )
    open_unrealized_error = final_unrealized_pnl - reconstructed_open_unrealized
    expected_equity = starting_balance + ledger_pnl + reconstructed_open_unrealized
    equity_error = ending_equity - expected_equity
    entry_error = int(candidate_signals) - entered - not_entered
    direction_error = int(candidate_signals) - int(long_signals) - int(short_signals)
    lifecycle_error = entered - count - len(open_positions)
    r_error = sum(
        trade.realised_r - (trade.pnl / trade.risk_amount)
        for trade in trades if trade.risk_amount > 0
    )
    reconciliation_passed = (
        missing_final_marks == 0
        and all(abs(value) <= 1e-7 for value in (
            wallet_error, open_unrealized_error, equity_error, entry_error,
            direction_error, lifecycle_error, r_error,
        ))
    )
    if start_time == 0 and end_time == 0 and valid_candles == 0:
        status = "NOT_RUN_NO_DATA"
    elif symbols_eligible == 0:
        status = "NOT_RUN_INSUFFICIENT_HISTORY"
    elif count == 0 and not open_positions:
        status = "COMPLETED_NO_TRADES"
    else:
        status = "COMPLETED"
    if not reconciliation_passed:
        status = "INVALID_RECONCILIATION"
    closed_liquidations = sum(t.exit_reason.startswith("LIQUIDATION") for t in trades)
    return {
        "status": status,
        "window": window,
        "mode": mode,
        "start_time": start_time,
        "end_time": end_time,
        "starting_balance": round(starting_balance, 8),
        "ending_balance": round(ending_balance, 8),
        "ending_equity": round(ending_equity, 8),
        "realized_pnl": round(ending_balance - starting_balance, 8),
        "final_unrealized_pnl": round(final_unrealized_pnl, 8),
        "total_marked_pnl": round(ending_equity - starting_balance, 8),
        "total_pnl": round(pnl_sum, 8),
        "return_pct": round((ending_equity / starting_balance - 1) * 100, 6),
        "symbols_tested": symbols_tested,
        "symbols_eligible": symbols_eligible,
        "valid_candles": valid_candles,
        "completed_trades": count,
        "positions_entered": entered,
        "candidate_signals": int(candidate_signals),
        "signals_not_entered": not_entered,
        "long_signals": int(long_signals),
        "short_signals": int(short_signals),
        "open_trades_at_end": len(open_positions),
        "take_profit_hits": sum(t.exit_reason.startswith("TARGET") for t in trades),
        "stop_loss_hits": sum(t.exit_reason.startswith("STOP") for t in trades),
        "long_take_profit_hits": sum(t.side == "LONG" and t.exit_reason.startswith("TARGET") for t in trades),
        "short_take_profit_hits": sum(t.side == "SHORT" and t.exit_reason.startswith("TARGET") for t in trades),
        "long_stop_loss_hits": sum(t.side == "LONG" and t.exit_reason.startswith("STOP") for t in trades),
        "short_stop_loss_hits": sum(t.side == "SHORT" and t.exit_reason.startswith("STOP") for t in trades),
        "time_stop_exits": sum(t.exit_reason == "TIME_STOP" for t in trades),
        "data_gap_exits": sum(t.exit_reason == "DATA_GAP_CONSERVATIVE" for t in trades),
        "liquidation_exits": closed_liquidations,
        "liquidation_events": int(liquidation_events),
        "unverified_liquidation_fee_positions": int(unverified_liquidation_fee_positions),
        "liquidation_modelled": bool(cross_margin_modelled),
        "cross_margin_maintenance_margin_modelled": bool(cross_margin_modelled),
        "cross_margin_modelled": bool(cross_margin_modelled),
        "liquidation_price_basis": liquidation_price_basis,
        "orderbook_impact_modelled": False,
        "max_observed_concurrent_positions": int(max_observed_concurrent_positions),
        "configured_max_concurrent_positions": int(configured_max_concurrent_positions),
        "max_initial_margin_used": round(max_initial_margin_used, 8),
        "max_notional_used": round(max_notional_used, 8),
        "long_trades": sum(t.side == "LONG" for t in trades),
        "short_trades": sum(t.side == "SHORT" for t in trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate_pct": round(win_rate, 4) if win_rate is not None else None,
        "average_win": round(statistics.fmean(t.pnl for t in wins), 8) if wins else None,
        "average_loss": round(statistics.fmean(t.pnl for t in losses), 8) if losses else None,
        "gross_profit": round(gross_win, 8),
        "gross_loss": round(gross_loss, 8),
        "profit_factor": round(gross_win / gross_loss, 6) if gross_loss > 0 else (None if not wins else "unbounded_no_losses"),
        "expectancy_per_trade": round(expectancy, 8) if expectancy is not None else None,
        "expectancy_r_per_trade": round(expectancy_r, 8) if expectancy_r is not None else None,
        "total_r": round(sum(r_values), 8),
        "average_realized_r": round(statistics.fmean(r_values), 8) if r_values else None,
        "average_signal_rr": round(statistics.fmean(signal_rr_values), 8) if signal_rr_values else None,
        "average_actual_fill_rr": round(statistics.fmean(fill_rr_values), 8) if fill_rr_values else None,
        "max_losing_streak": losing_streak,
        "average_mae_r": round(statistics.fmean(mae_values), 8) if mae_values else None,
        "average_mfe_r": round(statistics.fmean(mfe_values), 8) if mfe_values else None,
        "average_holding_hours": round(statistics.fmean([t.holding_hours for t in trades]), 8) if trades else None,
        "max_drawdown": round(max_dd, 8),
        "max_drawdown_pct": round(max_dd_pct, 6),
        "equity_curve": [round(float(value), 8) for value in equity_curve],
        "fees": round(sum(t.fees for t in trades) + sum(p.opening_fee + p.estimated_close_fee for p in open_positions), 8),
        "liquidation_fees": round(sum(t.liquidation_fee for t in trades), 8),
        "funding_pnl": round(sum(t.funding_pnl for t in trades) + sum(p.funding_pnl for p in open_positions), 8),
        "slippage_cost": round(sum(t.slippage_cost for t in trades) + sum(p.estimated_close_slippage for p in open_positions), 8),
        "rejection_reasons": dict(rejection_counts),
        "reconciliation": {
            "status": "PASS" if reconciliation_passed else "FAIL",
            "wallet_ledger_error": round(wallet_error, 10),
            "ending_equity_error": round(equity_error, 10),
            "open_unrealized_ledger_error": round(open_unrealized_error, 10),
            "missing_final_mark_count": int(missing_final_marks),
            "signal_entry_error": int(entry_error),
            "signal_direction_error": int(direction_error),
            "position_lifecycle_error": int(lifecycle_error),
            "trade_r_consistency_error": round(r_error, 10),
        },
        "warnings": list(dict.fromkeys(warnings)),
        "trades": [t.as_dict() for t in trades],
        "open_positions": [asdict(p) for p in open_positions],
    }


def settle_fixed_payout(balance: float, did_win: bool, win_payout: float = 20.0, loss_payout: float = 5.0) -> tuple[float, float, float]:
    """Apply the configured hypothetical payout and return (new_balance, pnl, R).

    This is not exchange P&L: costs are intentionally not deducted because the payout amount
    represents the whole modeled outcome in this scenario.
    """
    if win_payout < 0 or loss_payout < 0:
        raise ValueError("Payouts cannot be negative")
    pnl = win_payout if did_win else -loss_payout
    risk = loss_payout
    return balance + pnl, pnl, (pnl / risk if risk > 0 else 0.0)


def _position_from_candidate(candidate: TradeCandidate, candle: Candle, index: int, setup_key: str,
                             config: BacktestConfig, balance: float,
                             contract_spec: Mapping[str, Any] | None,
                             slippage_rate: float, fee_rate: float,
                             open_positions: Sequence[SimPosition]) -> tuple[SimPosition | None, str | None]:
    """Size a research position under a shared-wallet Cross Margin approximation.

    Initial margin is capped per position; it is not treated as the trade's dollar risk.
    The actual risk budget is derived from stop distance plus modeled round-trip costs.
    """
    raw_entry = float(candle.open)
    if (candidate.side == "LONG" and raw_entry <= candidate.stop_price) or (
        candidate.side == "SHORT" and raw_entry >= candidate.stop_price
    ):
        return None, "ENTRY_OPEN_CROSSED_STOP"
    if config.mode == "fixed_payout":
        if balance < config.fixed_loss_payout:
            return None, "INSUFFICIENT_FIXED_PAYOUT_BALANCE"
        entry = raw_entry
        target = entry * (1 + config.tp_pct / 100 if candidate.side == "LONG" else 1 - config.tp_pct / 100)
        stop = entry * (1 - config.sl_pct / 100 if candidate.side == "LONG" else 1 + config.sl_pct / 100)
        return SimPosition(candidate.symbol, candidate.side, index, candle.open_time, entry, stop, target,
            candidate.candidate_id, setup_key, config.fixed_loss_payout, mode=config.mode,
            funding_checked_until=candle.open_time), None
    if not contract_spec:
        return None, "MISSING_CONTRACT_SPEC"
    try:
        spec = contract_spec
        contract_size = float(spec.get("contractSize", spec.get("contract_size", 0)))
        vol_step = float(spec.get("volUnit", spec.get("vol_unit", 0)))
        min_vol = float(spec.get("minVol", spec.get("min_vol", 0)))
        max_vol = float(spec.get("maxVol", spec.get("max_vol", float("inf"))))
        max_leverage_raw = spec.get("countryConfigContractMaxLeverage", spec.get("maxLeverage"))
        mmr_raw = spec.get("maintenanceMarginRate", spec.get("maintenance_margin_rate", spec.get("mmr")))
        if max_leverage_raw is None:
            return None, "MISSING_CONTRACT_MAX_LEVERAGE"
        if mmr_raw is None:
            return None, "MISSING_MAINTENANCE_MARGIN_METADATA"
        contract_max_lev = int(float(max_leverage_raw))
        base_mmr = float(mmr_raw)
        liquidation_fee_rate = float(spec.get("liquidationFeeRate", spec.get("liquidation_fee_rate", config.liquidation_fee_rate_fallback)))
        if (min(contract_size, vol_step, min_vol, base_mmr) <= 0 or max_vol < min_vol
                or contract_max_lev < 1 or not 0 <= liquidation_fee_rate < 0.1):
            return None, "INVALID_CONTRACT_PRECISION_OR_RISK_METADATA"
        entry = _entry_price(raw_entry, candidate.side, slippage_rate)
        stop = float(candidate.stop_price)
        if (candidate.side == "LONG" and stop >= entry) or (candidate.side == "SHORT" and stop <= entry):
            return None, "STOP_INVALID_AFTER_ENTRY_GAP"

        stop_distance = abs(entry - stop)
        target = entry + (config.reward_risk_multiple * stop_distance if candidate.side == "LONG"
                          else -config.reward_risk_multiple * stop_distance)
        unit_notional_in = entry * contract_size
        unit_notional_out = max(target, stop, entry) * contract_size
        unit_stop = stop_distance * contract_size
        unit_fees = (unit_notional_in + unit_notional_out) * fee_rate
        unit_slippage = (unit_notional_in + unit_notional_out) * slippage_rate
        unit_risk = unit_stop + unit_fees + unit_slippage
        if unit_risk <= 0 or not math.isfinite(unit_risk):
            return None, "INVALID_MODELED_RISK"

        risk_budget = max(0.0, balance) * config.risk_fraction
        used_initial_margin = sum(p.initial_margin for p in open_positions)
        free_margin = max(0.0, balance - used_initial_margin)
        margin_budget = min(config.initial_margin_per_position, free_margin)
        if margin_budget <= 0:
            return None, "INSUFFICIENT_CROSS_MARGIN_AVAILABLE"
        total_open_notional = sum(p.quantity * p.contract_size * p.entry_price for p in open_positions)
        total_notional_cap = max(0.0, balance * config.max_total_notional_multiple - total_open_notional)
        open_risk = sum(p.risk_amount for p in open_positions)
        risk_cap = max(0.0, balance * config.max_open_risk_fraction - open_risk)
        if risk_cap <= 0:
            return None, "MAX_OPEN_RISK_EXCEEDED"

        leverage = min(config.max_leverage, contract_max_lev)
        raw_qty = min(
            risk_budget / unit_risk,
            risk_cap / unit_risk,
            margin_budget * leverage / unit_notional_in,
            total_notional_cap / unit_notional_in,
            max_vol,
        )
        qty = _floor_step(raw_qty, vol_step)
        tier_terms: dict[str, Any] = {"status": "BASE_MMR_FALLBACK", "mmr": base_mmr,
                                      "max_leverage": leverage, "tier": None}
        # Risk tiers can reduce allowed leverage as notional grows. Recompute the
        # permitted quantity until the tier and leverage agree, bounded to 5 passes.
        for _ in range(5):
            if qty < min_vol:
                return None, "BELOW_MINIMUM_RISK_SIZE"
            notional = qty * unit_notional_in
            tier_terms = contract_risk_tier_terms(
                spec, direction=candidate.side, quantity=qty, notional=notional,
                base_mmr=base_mmr, contract_max_leverage=contract_max_lev,
            )
            if tier_terms.get("status") == "OVER_LIMIT":
                return None, "CONTRACT_RISK_TIER_EXCEEDED"
            tier_leverage = max(1, min(config.max_leverage, int(tier_terms.get("max_leverage") or leverage)))
            revised = _floor_step(min(
                risk_budget / unit_risk,
                risk_cap / unit_risk,
                margin_budget * tier_leverage / unit_notional_in,
                total_notional_cap / unit_notional_in,
                max_vol,
            ), vol_step)
            if revised == qty:
                leverage = tier_leverage
                break
            qty = revised
            leverage = tier_leverage
        if qty < min_vol:
            return None, "BELOW_MINIMUM_RISK_SIZE"
        notional = qty * unit_notional_in
        initial_margin = notional / leverage
        if initial_margin > margin_budget + 1e-8:
            return None, "INITIAL_MARGIN_CAP_EXCEEDED"
        risk_amount = qty * unit_risk
        if risk_amount > risk_budget + 1e-8 or risk_amount > risk_cap + 1e-8:
            return None, "AGGREGATE_RISK_CAP_EXCEEDED"
        opening_fee = notional * fee_rate
        signal_rr = float(getattr(candidate, "gross_reward_risk", 0.0) or 0.0)
        net_target = qty * contract_size * abs(target - entry) - (
            (notional + qty * contract_size * target) * (fee_rate + slippage_rate)
        )
        actual_fill_rr = net_target / risk_amount if risk_amount > 0 else 0.0
        return SimPosition(
            symbol=candidate.symbol, side=candidate.side, entry_index=index,
            entry_time=candle.open_time, entry_price=entry, stop_price=stop,
            target_price=target, candidate_id=candidate.candidate_id, setup_key=setup_key,
            risk_amount=risk_amount, quantity=qty, contract_size=contract_size,
            opening_fee=opening_fee, funding_pnl=0.0, mode=config.mode,
            funding_checked_until=candle.open_time, initial_margin=initial_margin,
            leverage=float(leverage), maintenance_margin_rate=float(tier_terms.get("mmr", base_mmr)),
            liquidation_fee_rate=liquidation_fee_rate,
            estimated_close_fee=0.0, estimated_close_slippage=0.0,
            risk_tier_status=str(tier_terms.get("status", "BASE_MMR_FALLBACK")),
            risk_tier=tier_terms.get("tier"), signal_rr=signal_rr,
            actual_fill_rr=actual_fill_rr,
        ), None
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None, "INVALID_CONTRACT_OR_SIZE"


def _cross_margin_snapshot(
    wallet_balance: float,
    positions: Sequence[SimPosition],
    mark_prices: Mapping[str, float],
    fee_rate: float,
    slippage_rate: float = 0.0,
) -> dict[str, float]:
    """Estimate shared Cross Margin equity and maintenance requirements.

    This is not an exchange-exact liquidation calculation: the active S1-RFPC data
    path has 1H trade-price OHLC, not historical Fair Price and open-order streams.
    The caller supplies conservative adverse 1H OHLC proxy marks.
    """
    gross_upnl = 0.0
    estimated_close_fees = 0.0
    estimated_close_slippage = 0.0
    total_maintenance = 0.0
    total_liquidation_fees = 0.0
    total_notional = 0.0
    used_initial_margin = 0.0
    for pos in positions:
        mark = float(mark_prices.get(pos.symbol, pos.entry_price))
        direction = 1.0 if pos.side == "LONG" else -1.0
        notional = pos.quantity * pos.contract_size * mark
        gross_upnl += pos.quantity * pos.contract_size * (mark - pos.entry_price) * direction
        estimated_close_fees += notional * fee_rate
        estimated_close_slippage += notional * slippage_rate
        pos.estimated_close_fee = notional * fee_rate
        pos.estimated_close_slippage = notional * slippage_rate
        total_maintenance += notional * pos.maintenance_margin_rate
        total_liquidation_fees += notional * pos.liquidation_fee_rate
        total_notional += notional
        used_initial_margin += pos.initial_margin
    # Entry fees are removed from equity while positions remain open; funding is
    # already accrued into each position and is not deducted from wallet twice.
    open_costs_and_funding = sum(-p.opening_fee + p.funding_pnl for p in positions)
    equity = wallet_balance + gross_upnl + open_costs_and_funding - estimated_close_fees - estimated_close_slippage
    return {
        "equity": equity,
        "gross_unrealized_pnl": gross_upnl,
        "estimated_close_fees": estimated_close_fees,
        "estimated_close_slippage": estimated_close_slippage,
        "maintenance_margin": total_maintenance,
        "liquidation_fees": total_liquidation_fees,
        "maintenance_requirement": total_maintenance + total_liquidation_fees,
        "total_notional": total_notional,
        "used_initial_margin": used_initial_margin,
        "available_margin_estimate": equity - used_initial_margin,
    }


def run_backtest(
    timeframes_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    window: str = "7D",
    config: BacktestConfig | None = None,
    contract_specs: Mapping[str, Mapping[str, Any]] | None = None,
    funding_events: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """Backtest a chronological multi-symbol portfolio over a supplied historical dataset.

    Inputs must include pre-window indicator warm-up. A global timeline keeps trades across
    symbols in chronological order. Simultaneous new entries are handled in stable symbol order;
    that deterministic allocation convention is disclosed because shared equity is finite.
    """
    config = config or BacktestConfig()
    if window not in STANDARD_WINDOWS:
        raise ValueError(f"Unsupported window {window}; expected one of {', '.join(STANDARD_WINDOWS)}")
    all_bars = [c for series in timeframes_by_symbol.values() for vals in series.values() for c in vals if c.complete]
    starting_balance = config.starting_balance
    if not all_bars:
        empty = _metrics(window=window, mode=config.mode, start_time=0, end_time=0,
            starting_balance=starting_balance, ending_balance=starting_balance,
            trades=[], open_positions=[], equity_curve=[starting_balance], symbols_tested=len(timeframes_by_symbol),
            symbols_eligible=0, valid_candles=0, rejection_counts={"NO_DATA": len(timeframes_by_symbol)},
            warnings=["No completed candle data supplied; no results can be inferred."],
            max_observed_concurrent_positions=0,
            configured_max_concurrent_positions=config.max_concurrent_positions)
        empty.update({"strategy_evaluations": 0, "candidate_signals": 0, "entry_attempts": 0})
        return empty
    end_time = int(now_epoch or max(c.close_time for c in all_bars))
    start_time = end_time - STANDARD_WINDOWS[window] * 86400
    balance = starting_balance
    equity_curve: list[float] = [balance]
    completed: list[ClosedTrade] = []
    rejection_counts: dict[str, int] = {}
    warnings: list[str] = []
    used_setup_ids: set[str] = set()
    trade_counts: dict[str, int] = {symbol: 0 for symbol in timeframes_by_symbol}
    prepared: dict[str, dict[str, Any]] = {}
    timeline: dict[int, list[tuple[str, Candle]]] = {}
    strategy_evaluations = 0
    candidate_signals = 0
    entry_attempts = 0
    positions_entered = 0
    long_signals = 0
    short_signals = 0
    liquidation_events = 0
    unverified_liquidation_fee_positions = 0
    max_observed_concurrent_positions = 0
    max_initial_margin_used = 0.0
    max_notional_used = 0.0
    risk_tier_status_counts: dict[str, int] = {}

    for symbol, raw_series in timeframes_by_symbol.items():
        forbidden = set(raw_series) - {"1D", "12H", "4H", "1H"}
        if forbidden:
            raise ValueError(f"Forbidden strategy timeframe supplied for {symbol}: {sorted(forbidden)}")
        series = {tf: sorted(list(raw_series.get(tf, [])), key=lambda c: c.open_time) for tf in ("1D", "12H", "4H", "1H")}
        if not series["12H"] and series["4H"]:
            series["12H"] = aggregate_12h_from_4h(series["4H"])
        if not series["1H"] or len(series["1D"]) < 200:
            rejection_counts["INSUFFICIENT_HISTORY"] = rejection_counts.get("INSUFFICIENT_HISTORY", 0) + 1
            warnings.append(f"{symbol}: insufficient 1H data or 1D EMA200 warm-up; symbol excluded.")
            continue
        # Drop incomplete bars and reject duplicate open times; gap warnings remain visible.
        for tf in ("1D", "12H", "4H", "1H"):
            values = [c for c in series[tf] if c.complete]
            seen: set[int] = set()
            clean: list[Candle] = []
            for c in values:
                if c.open_time in seen:
                    raise ValueError(f"Duplicate {tf} candle for {symbol} at {c.open_time}")
                seen.add(c.open_time)
                clean.append(c)
            series[tf] = clean
        h1 = series["1H"]
        gap_count = sum(1 for a, b in zip(h1, h1[1:]) if b.open_time - a.open_time != 3600)
        if gap_count:
            warnings.append(f"{symbol}: {gap_count} gap(s) in 1H history; open positions are conservatively exited when a gap is encountered.")
        # All timeframe rows are now sorted, complete, and have unique opens.
        # Store immutable close/open timestamp indexes so each hourly decision
        # can find its causal prefixes with binary search instead of scanning all
        # history (including future candles) on every iteration.
        close_times = {
            tf: tuple(c.close_time for c in series[tf])
            for tf in ("1D", "12H", "4H", "1H")
        }
        open_times = tuple(c.open_time for c in h1)
        fast_path_validated = all(
            c.symbol == symbol and c.timeframe == tf
            for tf in ("1D", "12H", "4H", "1H")
            for c in series[tf]
        ) and all(
            all(left <= right for left, right in zip(close_times[tf], close_times[tf][1:]))
            for tf in ("1D", "12H", "4H", "1H")
        )
        prepared[symbol] = {
            "series": series,
            "bars1": h1,
            "index_by_open": {c.open_time: i for i, c in enumerate(h1)},
            "open_times": open_times,
            "close_times": close_times,
            "fast_path_validated": fast_path_validated,
        }
        for candle in h1:
            if candle.open_time < end_time and candle.close_time >= start_time:
                timeline.setdefault(candle.open_time, []).append((symbol, candle))

    symbols_eligible = len(prepared)
    valid_candles = sum(1 for p in prepared.values() for c in p["bars1"] if start_time <= c.close_time <= end_time)
    positions: dict[str, SimPosition] = {}
    pending: dict[str, tuple[TradeCandidate, str, int]] = {}
    pending_by_symbol: dict[str, tuple[TradeCandidate, str, int]] = {}
    slippage_rate = config.slippage_bps_per_side / 10000.0 * config.cost_multiplier
    fee_rate = config.taker_fee_rate * config.cost_multiplier

    for open_time in sorted(timeline):
        batch = sorted(timeline[open_time], key=lambda item: item[0])
        closed_this_batch: list[ClosedTrade] = []
        cross_liquidated_this_batch = False
        # 1) Detect missing 1H intervals for open positions. Fail conservatively at the first
        # available candle rather than silently allowing a position to survive a data void.
        for symbol, candle in batch:
            pos = positions.get(symbol)
            if pos and candle.open_time > pos.entry_time:
                prior_index = prepared[symbol]["index_by_open"].get(candle.open_time, 0)
                prev_bar = prepared[symbol]["bars1"][prior_index - 1] if prior_index > 0 else None
                if prev_bar and candle.open_time - prev_bar.open_time != 3600:
                    _accrue_funding_through(pos, candle, candle.open_time, funding_events)
                    raw_exit = min(candle.open, pos.stop_price) if pos.side == "LONG" else max(candle.open, pos.stop_price)
                    _update_excursion_at_price(pos, raw_exit)
                    closed_this_batch.append(_close_position(pos, candle, raw_exit, "DATA_GAP_CONSERVATIVE",
                        config, fee_rate, slippage_rate, funding_events))
                    positions.pop(symbol, None)
                    used_setup_ids.add(pos.setup_key)

        # Check existing portfolio at the new candle opens before admitting new positions.
        # This prevents a gapped account from increasing exposure before a margin breach is handled.
        if config.mode == "exchange_realistic" and positions:
            open_by_symbol = {symbol: candle.open for symbol, candle in batch}
            open_marks: dict[str, float] = {}
            for pos_symbol, pos in positions.items():
                if pos_symbol in open_by_symbol:
                    open_marks[pos_symbol] = float(open_by_symbol[pos_symbol])
                else:
                    history = prepared[pos_symbol]["bars1"]
                    prior_i = bisect_right(prepared[pos_symbol]["open_times"], open_time) - 1
                    open_marks[pos_symbol] = history[prior_i].close if prior_i >= 0 else pos.entry_price
            open_state = _cross_margin_snapshot(balance, list(positions.values()), open_marks, fee_rate, slippage_rate)
            if open_state["equity"] <= open_state["maintenance_requirement"]:
                liquidation_events += 1
                cross_liquidated_this_batch = True
                warnings.append(
                    "CROSS PORTFOLIO LIQUIDATION AT BAR OPEN: shared-wallet maintenance threshold breached "
                    "using 1H trade-price open proxy, not historical Fair Price."
                )
                for pos_symbol, pos in list(positions.items()):
                    current_bar = next((bar for sym, bar in batch if sym == pos_symbol), None)
                    if current_bar is None:
                        mark = float(open_marks[pos_symbol])
                        current_bar = Candle(
                            pos_symbol, "1H", open_time, mark, mark, mark, mark,
                            volume=0.0, source="cross_margin_stale_mark_proxy",
                        )
                    _update_excursion_at_price(pos, open_marks[pos_symbol])
                    _accrue_funding_through(pos, current_bar, open_time, funding_events)
                    closed_this_batch.append(_close_position(
                        pos, current_bar, open_marks[pos_symbol], "LIQUIDATION_CROSS_PORTFOLIO_AT_OPEN",
                        config, fee_rate, slippage_rate, funding_events,
                    ))
                    positions.pop(pos_symbol, None)
                    used_setup_ids.add(pos.setup_key)

        # 2) Fill intents at the next 1H open only. A missing next bar cancels the intent.
        entry_candidates: list[tuple[str, TradeCandidate, str, int, Candle]] = []
        for symbol, candle in batch:
            intent = pending_by_symbol.pop(symbol, None)
            if intent:
                candidate, setup_key, expected_entry_time = intent
                if candle.open_time != expected_entry_time:
                    rejection_counts["MISSED_NEXT_BAR"] = rejection_counts.get("MISSED_NEXT_BAR", 0) + 1
                else:
                    entry_candidates.append((symbol, candidate, setup_key, expected_entry_time, candle))
        for symbol, candidate, setup_key, expected_entry_time, candle in sorted(
            entry_candidates, key=lambda row: (-float(row[1].gross_reward_risk), row[0])
        ):
            entry_attempts += 1
            if cross_liquidated_this_batch:
                rejection_counts["CANCELLED_AFTER_CROSS_LIQUIDATION"] = rejection_counts.get("CANCELLED_AFTER_CROSS_LIQUIDATION", 0) + 1
                continue
            if symbol in positions or len(positions) >= config.max_concurrent_positions:
                rejection_counts["MAX_CONCURRENT_POSITIONS"] = rejection_counts.get("MAX_CONCURRENT_POSITIONS", 0) + 1
                continue
            if config.mode == "fixed_payout" and balance < config.fixed_loss_payout:
                rejection_counts["INSUFFICIENT_FIXED_PAYOUT_BALANCE"] = rejection_counts.get("INSUFFICIENT_FIXED_PAYOUT_BALANCE", 0) + 1
                continue
            history = prepared[symbol]["bars1"]
            signal_index = prepared[symbol]["index_by_open"].get(candidate.trigger_time)
            if signal_index is not None and signal_index >= 13:
                hist = history[:signal_index + 1]
                atr_values = atr([x.high for x in hist], [x.low for x in hist], [x.close for x in hist], 14)
                current_atr = atr_values[-1]
                if current_atr and abs(candle.open - candidate.entry_reference) > 0.50 * current_atr:
                    rejection_counts["ENTRY_GAP_EXCEEDED"] = rejection_counts.get("ENTRY_GAP_EXCEEDED", 0) + 1
                    continue
            live_risk = sum(p.risk_amount for p in positions.values())
            current_open_marks: dict[str, float] = {}
            batch_open_by_symbol = {sym: row.open for sym, row in batch}
            for pos_symbol, open_pos in positions.items():
                if pos_symbol in batch_open_by_symbol:
                    current_open_marks[pos_symbol] = float(batch_open_by_symbol[pos_symbol])
                else:
                    history_pos = prepared[pos_symbol]["bars1"]
                    prior_i = bisect_right(prepared[pos_symbol]["open_times"], open_time) - 1
                    current_open_marks[pos_symbol] = history_pos[prior_i].close if prior_i >= 0 else open_pos.entry_price
            current_account = _cross_margin_snapshot(
                balance, list(positions.values()), current_open_marks, fee_rate, slippage_rate,
            ) if config.mode == "exchange_realistic" and positions else {"equity": balance}
            sizing_equity = max(0.0, float(current_account["equity"]))
            if config.mode == "exchange_realistic" and live_risk >= sizing_equity * config.max_open_risk_fraction:
                rejection_counts["MAX_OPEN_RISK_EXCEEDED"] = rejection_counts.get("MAX_OPEN_RISK_EXCEEDED", 0) + 1
                continue
            entry_index = prepared[symbol]["index_by_open"].get(candle.open_time, signal_index or 0)
            pos, error = _position_from_candidate(candidate, candle, entry_index, setup_key, config, sizing_equity,
                (contract_specs or {}).get(symbol), slippage_rate, fee_rate, list(positions.values()))
            if error or pos is None:
                reason = error or "ENTRY_REJECTED"
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                continue
            positions[symbol] = pos
            positions_entered += 1
            if pos.liquidation_fee_rate <= 0:
                unverified_liquidation_fee_positions += 1
            risk_tier_status_counts[pos.risk_tier_status] = risk_tier_status_counts.get(pos.risk_tier_status, 0) + 1
            max_observed_concurrent_positions = max(max_observed_concurrent_positions, len(positions))
            current_initial_margin = sum(p.initial_margin for p in positions.values())
            current_notional = sum(p.quantity * p.contract_size * p.entry_price for p in positions.values())
            max_initial_margin_used = max(max_initial_margin_used, current_initial_margin)
            max_notional_used = max(max_notional_used, current_notional)

        # 3) Evaluate a conservative portfolio-level Cross Margin threshold before
        # ordinary SL/TP processing. Historical Fair Price bars are not part of this
        # S1-RFPC feed, so 1H trade-price adverse extremes are explicitly a stress proxy.
        if config.mode == "exchange_realistic" and positions and not cross_liquidated_this_batch:
            candle_by_symbol = {symbol: candle for symbol, candle in batch}
            adverse_prices: dict[str, float] = {}
            for pos_symbol, pos in list(positions.items()):
                current_bar = candle_by_symbol.get(pos_symbol)
                if current_bar is None:
                    history = prepared[pos_symbol]["bars1"]
                    prior_i = bisect_right(prepared[pos_symbol]["open_times"], open_time) - 1
                    # A symbol without a candle at this timestamp contributes its last
                    # known close, not the previous candle's old extremes a second time.
                    adverse = history[prior_i].close if prior_i >= 0 else pos.entry_price
                else:
                    adverse = current_bar.low if pos.side == "LONG" else current_bar.high
                adverse_prices[pos_symbol] = float(adverse)
            margin_state = _cross_margin_snapshot(balance, list(positions.values()), adverse_prices, fee_rate, slippage_rate)
            if margin_state["equity"] <= margin_state["maintenance_requirement"]:
                liquidation_events += 1
                cross_liquidated_this_batch = True
                warnings.append(
                    "CROSS PORTFOLIO LIQUIDATION EVENT: conservative 1H trade-price OHLC stress proxy; "
                    "not an exchange-exact Fair Price/partial-liquidation replay."
                )
                for pos_symbol, pos in list(positions.items()):
                    current_bar = candle_by_symbol.get(pos_symbol)
                    if current_bar is None:
                        mark = float(adverse_prices[pos_symbol])
                        current_bar = Candle(
                            pos_symbol, "1H", open_time, mark, mark, mark, mark,
                            volume=0.0, source="cross_margin_stale_mark_proxy",
                        )
                    _update_excursion_at_price(pos, adverse_prices[pos_symbol])
                    _accrue_funding_through(pos, current_bar, open_time, funding_events)
                    closed_this_batch.append(_close_position(
                        pos, current_bar, adverse_prices[pos_symbol], "LIQUIDATION_CROSS_PORTFOLIO_OHLC_STRESS",
                        config, fee_rate, slippage_rate, funding_events,
                    ))
                    positions.pop(pos_symbol, None)
                    used_setup_ids.add(pos.setup_key)

        # 4) Resolve ordinary exits for every remaining position; a portfolio liquidation
        # removes every cross position before this stage and cannot be double-counted.
        # The pre-registered 240-hour maximum holding period exits at the first available 1H open
        # at or after expiry. No unpermitted finer candles are used to resolve the exit.
        for symbol, candle in batch:
            pos = positions.get(symbol)
            if pos is None or candle.open_time < pos.entry_time:
                continue
            if candle.open_time - pos.entry_time >= config.max_holding_hours * 3600:
                _update_excursion_at_price(pos, candle.open)
                _accrue_funding_through(pos, candle, candle.open_time, funding_events)
                closed_this_batch.append(_close_position(pos, candle, candle.open, "TIME_STOP",
                    config, fee_rate, slippage_rate, funding_events))
                positions.pop(symbol, None)
                used_setup_ids.add(pos.setup_key)
                continue
            outcome = _outcome_on_bar(pos, candle)
            if outcome:
                raw_exit, reason = outcome
                # OHLC cannot reveal the path before an intrabar exit. Record only the
                # known exit price; full candle extremes could occur after the position closed.
                _update_excursion_at_price(pos, raw_exit)
                if reason.endswith("_GAP"):
                    _accrue_funding_through(pos, candle, candle.open_time, funding_events)
                else:
                    _accrue_funding_through(pos, candle, candle.close_time, funding_events,
                                            allow_favorable=False)
                closed_this_batch.append(_close_position(pos, candle, raw_exit, reason, config, fee_rate, slippage_rate, funding_events))
                positions.pop(symbol, None)
                used_setup_ids.add(pos.setup_key)
            else:
                _update_excursions_from_open_bar(pos, candle)
                _accrue_funding_through(pos, candle, candle.close_time, funding_events)
        if closed_this_batch:
            for trade in closed_this_batch:
                balance += trade.pnl
                completed.append(trade)
                trade_counts[trade.symbol] = trade_counts.get(trade.symbol, 0) + 1

        # Mark-to-market unrealised equity for realistic P&L. Fixed-payout mode deliberately has
        # no continuous dollar mark because its $20/$5 contract is a discrete hypothetical payout.
        if config.mode == "exchange_realistic":
            close_by_symbol = {symbol: candle.close for symbol, candle in batch}
            unrealised = 0.0
            for pos_symbol, pos in positions.items():
                mark = close_by_symbol.get(pos_symbol)
                if mark is None:
                    history = prepared[pos_symbol]["bars1"]
                    prior_index = bisect_right(
                        prepared[pos_symbol]["open_times"], open_time
                    ) - 1
                    mark = history[prior_index].close if prior_index >= 0 else pos.entry_price
                direction = 1.0 if pos.side == "LONG" else -1.0
                notional_out = pos.quantity * pos.contract_size * mark
                gross_unrealised = pos.quantity * pos.contract_size * (mark - pos.entry_price) * direction
                est_close_fee = notional_out * fee_rate
                est_close_slippage = notional_out * slippage_rate
                pos.estimated_close_fee = est_close_fee
                pos.estimated_close_slippage = est_close_slippage
                unrealised += gross_unrealised - pos.opening_fee - est_close_fee - est_close_slippage + pos.funding_pnl
            equity_curve.append(balance + unrealised)
        else:
            equity_curve.append(balance)

        # 4) Evaluate signals after the bars in this batch have closed. Inputs are clipped to
        # close_time <= this timestamp; the entry is scheduled at that close (next 1H open).
        for symbol, candle in batch:
            if candle.close_time < start_time or candle.close_time > end_time:
                continue
            if symbol in positions or symbol in pending_by_symbol:
                continue
            if trade_counts.get(symbol, 0) >= config.max_trades_per_symbol:
                continue
            series = prepared[symbol]["series"]
            decision_time = candle.close_time
            if prepared[symbol]["fast_path_validated"]:
                snapshot: dict[str, Sequence[Candle]] = {
                    tf: series[tf][
                        :bisect_right(prepared[symbol]["close_times"][tf], decision_time)
                    ]
                    for tf in ("1D", "12H", "4H", "1H")
                }
            else:
                # Retain the defensive filter for malformed/non-monotonic input.
                snapshot = {
                    tf: [c for c in series[tf] if c.complete and c.close_time <= decision_time]
                    for tf in ("1D", "12H", "4H", "1H")
                }
            if not snapshot["12H"] and snapshot["4H"]:
                snapshot["12H"] = aggregate_12h_from_4h(snapshot["4H"])
            strategy_evaluations += 1
            try:
                evaluation = evaluate_strategy(
                    symbol,
                    snapshot,
                    decision_time=decision_time,
                    prevalidated=bool(prepared[symbol]["fast_path_validated"]),
                    target_r_multiple=config.reward_risk_multiple,
                )
            except Exception as exc:
                warnings.append(f"{symbol}: strategy evaluation failed at {decision_time} ({type(exc).__name__}); candidate rejected.")
                rejection_counts["STRATEGY_EVALUATION_ERROR"] = rejection_counts.get("STRATEGY_EVALUATION_ERROR", 0) + 1
                continue
            if evaluation.candidate:
                candidate_signals += 1
                if evaluation.candidate.side == "LONG":
                    long_signals += 1
                elif evaluation.candidate.side == "SHORT":
                    short_signals += 1
                candidate = evaluation.candidate
                setup_key = f"{candidate.symbol}|{candidate.side}|{candidate.setup_pivot_time}|{candidate.breakout_time}|{candidate.reclaim_time}"
                if setup_key not in used_setup_ids:
                    pending_by_symbol[symbol] = (candidate, setup_key, candle.close_time)
                else:
                    rejection_counts["DUPLICATE_SETUP_SUPPRESSED"] = rejection_counts.get("DUPLICATE_SETUP_SUPPRESSED", 0) + 1
            elif evaluation.rejection_reasons:
                reason = evaluation.rejection_reasons[0]
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    open_positions = list(positions.values())
    final_unrealized_pnl = 0.0
    if config.mode == "exchange_realistic" and open_positions:
        for pos in open_positions:
            history = prepared[pos.symbol]["bars1"]
            last_i = bisect_right(prepared[pos.symbol]["open_times"], end_time) - 1
            mark = history[last_i].close if last_i >= 0 else pos.entry_price
            direction = 1.0 if pos.side == "LONG" else -1.0
            notional = pos.quantity * pos.contract_size * mark
            gross = pos.quantity * pos.contract_size * (mark - pos.entry_price) * direction
            est_close_fee = notional * fee_rate
            est_close_slippage = notional * slippage_rate
            pos.estimated_close_fee = est_close_fee
            pos.estimated_close_slippage = est_close_slippage
            pos.final_mark_price = float(mark)
            final_unrealized_pnl += gross - pos.opening_fee - est_close_fee - est_close_slippage + pos.funding_pnl
    ending_equity = balance + final_unrealized_pnl
    if open_positions and config.mode == "exchange_realistic":
        equity_curve.append(ending_equity)
    if pending_by_symbol:
        warnings.append(f"{len(pending_by_symbol)} candidate(s) remained unfilled at the end of the window; not counted as entered positions.")
    if config.mode == "exchange_realistic":
        warnings.append("Exchange-realistic mode is modeled, not observed execution. Verify applicable fees, funding coverage, contract-size units and price-series semantics before interpreting results.")
        # Funding can affect P&L only while a position is open. Do not warn about
        # missing settlements when this run never opened a position.
        if completed or open_positions:
            traded_symbols = {trade.symbol for trade in completed} | {pos.symbol for pos in open_positions}
            missing_funding = [s for s in sorted(traded_symbols) if not funding_events or not funding_events.get(s)]
            if missing_funding:
                warnings.append("Funding event history is missing/empty for: " + ", ".join(sorted(missing_funding)) + "; realized P&L excludes unknown funding and is not validation-ready.")
    else:
        warnings.append("Fixed-payout mode is hypothetical: TP/SL price distances select outcome; +win/-loss payouts are credited without exchange fees and are not MEXC trade economics.")
        warnings.append("Fixed-payout maximum drawdown is based on realised fixed-payout equity; it does not mark open trades to market.")
    if not symbols_eligible:
        warnings.append("No eligible symbol had sufficient historical warm-up data; no strategy performance conclusion is possible.")
    warnings.append(f"Strategy simulation maximum holding period: {config.max_holding_hours} hours; time-stop is modeled at the next available 1H open.")
    if config.mode == "exchange_realistic":
        warnings.append(
            f"CROSS research model: max {config.max_concurrent_positions} positions, "
            f"${config.initial_margin_per_position:.2f} maximum initial margin per position, "
            f"max leverage {config.max_leverage}x, total notional cap {config.max_total_notional_multiple:.1f}x wallet, "
            f"aggregate stop-risk cap {config.max_open_risk_fraction:.1%}."
        )
        warnings.append(
            "Liquidation uses a conservative full-portfolio 1H trade-price OHLC stress proxy, not historical Fair Price; "
            "MEXC partial liquidation, open orders, external positions, ADL and order-book impact are not replayed."
        )
        if any(status != "CONTRACT_METADATA_TIER" for status in risk_tier_status_counts):
            warnings.append("Some positions used base contract MMR fallback because public metadata did not expose a complete tier schedule.")
        if unverified_liquidation_fee_positions:
            warnings.append(
                f"{unverified_liquidation_fee_positions} entered position(s) used no verified liquidation-fee rate; "
                "liquidation cost may be understated."
            )
    if open_positions:
        warnings.append("Open positions are marked to market at the final available 1H close; estimated closing fee is included in unrealized P&L.")
    metrics = _metrics(
        window=window, mode=config.mode, start_time=start_time, end_time=end_time,
        starting_balance=starting_balance, ending_balance=balance, trades=completed,
        open_positions=open_positions, equity_curve=equity_curve, symbols_tested=len(timeframes_by_symbol),
        symbols_eligible=symbols_eligible, valid_candles=valid_candles,
        rejection_counts=rejection_counts, warnings=warnings,
        max_observed_concurrent_positions=max_observed_concurrent_positions,
        configured_max_concurrent_positions=config.max_concurrent_positions,
        positions_entered=positions_entered, candidate_signals=candidate_signals,
        long_signals=long_signals, short_signals=short_signals,
        final_unrealized_pnl=final_unrealized_pnl,
        max_initial_margin_used=max_initial_margin_used,
        max_notional_used=max_notional_used,
        liquidation_events=liquidation_events,
        unverified_liquidation_fee_positions=unverified_liquidation_fee_positions,
        cross_margin_modelled=config.mode == "exchange_realistic",
        liquidation_price_basis=("1H_TRADE_PRICE_OHLC_CONSERVATIVE_STRESS_PROXY" if config.mode == "exchange_realistic" else "NOT_MODELLED"),
    )
    metrics.update({
        "strategy_evaluations": strategy_evaluations,
        "entry_attempts": entry_attempts,
        "risk_tier_status_counts": risk_tier_status_counts,
        "margin_mode": config.margin_mode if config.mode == "exchange_realistic" else "N/A",
        "configured_initial_margin_cap_per_position": config.initial_margin_per_position,
        "configured_max_leverage": config.max_leverage,
        "configured_total_notional_multiple": config.max_total_notional_multiple,
        "configured_max_open_risk_fraction": config.max_open_risk_fraction,
        "simultaneous_entry_priority": "gross_reward_risk_desc_then_symbol_asc",
        "exchange_exact_cross_margin_modelled": False,
        "cross_margin_model_type": "SHARED_WALLET_MAINTENANCE_STRESS_PROXY",
        "final_cross_margin_snapshot": _cross_margin_snapshot(
            balance, open_positions,
            {p.symbol: (float(p.final_mark_price) if p.final_mark_price is not None else p.entry_price)
             for p in open_positions}, fee_rate, slippage_rate,
        ) if open_positions and config.mode == "exchange_realistic" else None,
    })
    return metrics


def _close_position(pos: SimPosition, candle: Candle, raw_exit: float, exit_reason: str,
                    config: BacktestConfig, fee_rate: float, slippage_rate: float,
                    funding_events: Mapping[str, Sequence[Mapping[str, Any]]] | None) -> ClosedTrade:
    exit_px = _exit_price(raw_exit, pos.side, slippage_rate) if config.mode == "exchange_realistic" else raw_exit
    exit_time = (candle.open_time if exit_reason.endswith("_GAP") or
                 exit_reason in {"TIME_STOP", "DATA_GAP_CONSERVATIVE", "LIQUIDATION_CROSS_PORTFOLIO_AT_OPEN"}
                 else candle.close_time)
    liquidation_fee = 0.0
    if config.mode == "fixed_payout":
        if exit_reason.startswith("TARGET"):
            won = True
        elif exit_reason.startswith(("STOP", "LIQUIDATION")):
            won = False
        else:
            direction = 1.0 if pos.side == "LONG" else -1.0
            directional_move = (raw_exit - pos.entry_price) * direction
            if abs(directional_move) <= 1e-12:
                pnl, realised_r = 0.0, 0.0
                fees = funding_pnl = slip_cost = 0.0
                risk_amount = config.fixed_loss_payout
                return ClosedTrade(pos.symbol, pos.side, pos.entry_time, exit_time, pos.entry_price,
                    exit_px, exit_reason, pnl, realised_r, risk_amount, pos.candidate_id, config.mode,
                    fees, funding_pnl, slip_cost)
            won = directional_move > 0
        _, pnl, realised_r = settle_fixed_payout(0.0, won, config.fixed_win_payout, config.fixed_loss_payout)
        fees = funding_pnl = slip_cost = 0.0
        risk_amount = config.fixed_loss_payout
    else:
        direction = 1.0 if pos.side == "LONG" else -1.0
        notional_in = pos.quantity * pos.contract_size * pos.entry_price
        notional_out = pos.quantity * pos.contract_size * exit_px
        gross_pnl = pos.quantity * pos.contract_size * (exit_px - pos.entry_price) * direction
        closing_fee = notional_out * fee_rate
        fees = pos.opening_fee + closing_fee
        funding_pnl = pos.funding_pnl
        slip_cost = (notional_in + notional_out) * slippage_rate
        if exit_reason.startswith("LIQUIDATION"):
            liquidation_fee = notional_out * pos.liquidation_fee_rate
        pnl = gross_pnl + funding_pnl - fees - liquidation_fee
        risk_amount = pos.risk_amount
        realised_r = pnl / risk_amount if risk_amount > 0 else 0.0
    holding_hours = max(0.0, (exit_time - pos.entry_time) / 3600.0)
    return ClosedTrade(
        pos.symbol, pos.side, pos.entry_time, exit_time, pos.entry_price, exit_px,
        exit_reason, pnl, realised_r, risk_amount, pos.candidate_id, config.mode,
        fees, funding_pnl, slip_cost, liquidation_fee, pos.signal_rr,
        pos.actual_fill_rr, pos.mae_r, pos.mfe_r, holding_hours,
        quantity=pos.quantity, contract_size=pos.contract_size,
        initial_margin=pos.initial_margin, leverage=pos.leverage,
        maintenance_margin_rate=pos.maintenance_margin_rate,
        liquidation_fee_rate=pos.liquidation_fee_rate,
        risk_tier_status=pos.risk_tier_status, risk_tier=pos.risk_tier,
        margin_mode=config.margin_mode if config.mode == "exchange_realistic" else "N/A",
    )


def run_all_standard_windows(
    timeframes_by_symbol: Mapping[str, Mapping[str, Sequence[Candle]]],
    *,
    mode: str = "fixed_payout",
    config: BacktestConfig | None = None,
    contract_specs: Mapping[str, Mapping[str, Any]] | None = None,
    funding_events: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    now_epoch: int | None = None,
) -> list[dict[str, Any]]:
    base = config or BacktestConfig(mode=mode)
    results = []
    for window in STANDARD_WINDOWS:
        window_config = BacktestConfig(**{**asdict(base), "mode": mode, "starting_balance": base.starting_balance})
        results.append(run_backtest(timeframes_by_symbol, window=window, config=window_config,
            contract_specs=contract_specs, funding_events=funding_events, now_epoch=now_epoch))
    return results
