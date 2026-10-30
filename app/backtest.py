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
    max_holding_hours: int = 240
    reward_risk_multiple: float = 2.0

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
        if self.max_holding_hours < 1:
            raise ValueError("max_holding_hours must be positive")
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
    max_dd, max_dd_pct = _drawdown(equity_curve)
    if start_time == 0 and end_time == 0 and valid_candles == 0:
        status = "NOT_RUN_NO_DATA"
    elif symbols_eligible == 0:
        status = "NOT_RUN_INSUFFICIENT_HISTORY"
    elif count == 0 and not open_positions:
        status = "COMPLETED_NO_TRADES"
    else:
        status = "COMPLETED"
    return {
        "status": status,
        "window": window,
        "mode": mode,
        "start_time": start_time,
        "end_time": end_time,
        "starting_balance": round(starting_balance, 8),
        "ending_balance": round(ending_balance, 8),
        "total_pnl": round(pnl_sum, 8),
        "return_pct": round((ending_balance / starting_balance - 1) * 100, 6),
        "symbols_tested": symbols_tested,
        "symbols_eligible": symbols_eligible,
        "valid_candles": valid_candles,
        "completed_trades": count,
        "open_trades_at_end": len(open_positions),
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
        "max_drawdown": round(max_dd, 8),
        "max_drawdown_pct": round(max_dd_pct, 6),
        "equity_curve": [round(float(value), 8) for value in equity_curve],
        "fees": round(sum(t.fees for t in trades), 8),
        "funding_pnl": round(sum(t.funding_pnl for t in trades), 8),
        "slippage_cost": round(sum(t.slippage_cost for t in trades), 8),
        "rejection_reasons": dict(rejection_counts),
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
    raw_entry = candle.open
    # Gap size is checked in the orchestration loop using ATR known at the trigger close.
    if (candidate.side == "LONG" and raw_entry <= candidate.stop_price) or (candidate.side == "SHORT" and raw_entry >= candidate.stop_price):
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
        contract_size = float(contract_spec.get("contractSize", contract_spec.get("contract_size", 0)))
        vol_step = float(contract_spec.get("volUnit", contract_spec.get("vol_unit", 0)))
        min_vol = float(contract_spec.get("minVol", contract_spec.get("min_vol", 0)))
        max_vol = float(contract_spec.get("maxVol", contract_spec.get("max_vol", float("inf"))))
        if min(contract_size, vol_step, min_vol) <= 0 or max_vol < min_vol:
            return None, "INVALID_CONTRACT_PRECISION"
        entry = _entry_price(raw_entry, candidate.side, slippage_rate)
        stop = candidate.stop_price
        if (candidate.side == "LONG" and stop >= entry) or (candidate.side == "SHORT" and stop <= entry):
            return None, "STOP_INVALID_AFTER_ENTRY_GAP"
        stop_distance = abs(entry - stop)
        risk_budget = max(0.0, balance) * config.risk_fraction
        unit_stop = stop_distance * contract_size
        target = entry + (config.reward_risk_multiple * stop_distance if candidate.side == "LONG" else -config.reward_risk_multiple * stop_distance)
        unit_fee = entry * contract_size * fee_rate + max(target, stop, entry) * contract_size * fee_rate
        unit_slippage = (entry + max(target, stop)) * contract_size * slippage_rate
        unit_risk = unit_stop + unit_fee + unit_slippage
        if unit_risk <= 0:
            return None, "INVALID_MODELED_RISK"
        notional_cap = max(0.0, balance * 3.0 - sum(p.quantity * p.contract_size * p.entry_price for p in open_positions))
        risk_cap = max(0.0, balance * 0.02 - sum(p.risk_amount for p in open_positions))
        raw_qty = min(risk_budget / unit_risk, notional_cap / (entry * contract_size), max_vol)
        qty = _floor_step(raw_qty, vol_step)
        while qty >= min_vol and (qty * unit_risk > risk_budget + 1e-9 or qty * unit_stop > risk_cap + 1e-9):
            qty = round(qty - vol_step, 12)
        if qty < min_vol:
            return None, "BELOW_MINIMUM_RISK_SIZE"
        notional = qty * contract_size * entry
        opening_fee = notional * fee_rate
        return SimPosition(candidate.symbol, candidate.side, index, candle.open_time, entry, stop, target,
            candidate.candidate_id, setup_key, risk_budget, qty, contract_size, opening_fee, 0.0, config.mode,
            funding_checked_until=candle.open_time), None
    except (TypeError, ValueError, OverflowError):
        return None, "INVALID_CONTRACT_OR_SIZE"


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
            warnings=["No completed candle data supplied; no results can be inferred."])
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
                    closed_this_batch.append(_close_position(pos, candle, raw_exit, "DATA_GAP_CONSERVATIVE",
                        config, fee_rate, slippage_rate, funding_events))
                    positions.pop(symbol, None)
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
        for symbol, candidate, setup_key, expected_entry_time, candle in entry_candidates:
            entry_attempts += 1
            if symbol in positions or len(positions) >= 4:
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
            if config.mode == "exchange_realistic" and live_risk >= balance * 0.02:
                rejection_counts["MAX_OPEN_RISK_EXCEEDED"] = rejection_counts.get("MAX_OPEN_RISK_EXCEEDED", 0) + 1
                continue
            entry_index = prepared[symbol]["index_by_open"].get(candle.open_time, signal_index or 0)
            pos, error = _position_from_candidate(candidate, candle, entry_index, setup_key, config, balance,
                (contract_specs or {}).get(symbol), slippage_rate, fee_rate, list(positions.values()))
            if error or pos is None:
                reason = error or "ENTRY_REJECTED"
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                continue
            positions[symbol] = pos

        # 3) Resolve exits for every position in the same timestamp batch, then apply PnL together.
        # The pre-registered 240-hour maximum holding period exits at the first available 1H open
        # at or after expiry. No unpermitted finer candles are used to resolve the exit.
        for symbol, candle in batch:
            pos = positions.get(symbol)
            if pos is None or candle.open_time < pos.entry_time:
                continue
            if candle.open_time - pos.entry_time >= config.max_holding_hours * 3600:
                _accrue_funding_through(pos, candle, candle.open_time, funding_events)
                closed_this_batch.append(_close_position(pos, candle, candle.open, "TIME_STOP",
                    config, fee_rate, slippage_rate, funding_events))
                positions.pop(symbol, None)
                used_setup_ids.add(pos.setup_key)
                continue
            outcome = _outcome_on_bar(pos, candle)
            if outcome:
                raw_exit, reason = outcome
                if reason.endswith("_GAP"):
                    _accrue_funding_through(pos, candle, candle.open_time, funding_events)
                else:
                    _accrue_funding_through(pos, candle, candle.close_time, funding_events,
                                            allow_favorable=False)
                closed_this_batch.append(_close_position(pos, candle, raw_exit, reason, config, fee_rate, slippage_rate, funding_events))
                positions.pop(symbol, None)
                used_setup_ids.add(pos.setup_key)
            else:
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
                unrealised += gross_unrealised - pos.opening_fee - est_close_fee + pos.funding_pnl
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
    if pending_by_symbol:
        warnings.append(f"{len(pending_by_symbol)} candidate(s) remained unfilled at the end of the window; not counted as trades.")
    if config.mode == "exchange_realistic":
        warnings.append("Exchange-realistic mode is modeled, not observed execution. Verify applicable fees, funding coverage, contract-size units and price-series semantics before interpreting results.")
        # Funding can affect P&L only while a position is open. Do not warn about
        # missing settlements when this run never opened a position.
        if completed or open_positions:
            missing_funding = [s for s in prepared if not funding_events or not funding_events.get(s)]
            if missing_funding:
                warnings.append("Funding event history is missing/empty for: " + ", ".join(sorted(missing_funding)) + "; realized P&L excludes unknown funding and is not validation-ready.")
    else:
        warnings.append("Fixed-payout mode is hypothetical: TP/SL price distances select outcome; +win/-loss payouts are credited without exchange fees and are not MEXC trade economics.")
        warnings.append("Fixed-payout maximum drawdown is based on realised fixed-payout equity; it does not mark open trades to market.")
    if not symbols_eligible:
        warnings.append("No eligible symbol had sufficient historical warm-up data; no strategy performance conclusion is possible.")
    warnings.append(f"Strategy simulation maximum holding period: {config.max_holding_hours} hours; time-stop is modeled at the next available 1H open.")
    metrics = _metrics(
        window=window, mode=config.mode, start_time=start_time, end_time=end_time,
        starting_balance=starting_balance, ending_balance=balance, trades=completed,
        open_positions=open_positions, equity_curve=equity_curve, symbols_tested=len(timeframes_by_symbol),
        symbols_eligible=symbols_eligible, valid_candles=valid_candles,
        rejection_counts=rejection_counts, warnings=warnings,
    )
    metrics.update({
        "strategy_evaluations": strategy_evaluations,
        "candidate_signals": candidate_signals,
        "entry_attempts": entry_attempts,
    })
    return metrics


def _close_position(pos: SimPosition, candle: Candle, raw_exit: float, exit_reason: str,
                    config: BacktestConfig, fee_rate: float, slippage_rate: float,
                    funding_events: Mapping[str, Sequence[Mapping[str, Any]]] | None) -> ClosedTrade:
    exit_px = _exit_price(raw_exit, pos.side, slippage_rate) if config.mode == "exchange_realistic" else raw_exit
    if config.mode == "fixed_payout":
        if exit_reason.startswith("TARGET"):
            won = True
        elif exit_reason.startswith("STOP"):
            won = False
        else:
            direction = 1.0 if pos.side == "LONG" else -1.0
            directional_move = (raw_exit - pos.entry_price) * direction
            if abs(directional_move) <= 1e-12:
                pnl, realised_r = 0.0, 0.0
                fees = funding_pnl = slip_cost = 0.0
                risk_amount = config.fixed_loss_payout
                return ClosedTrade(pos.symbol, pos.side, pos.entry_time, candle.open_time, pos.entry_price,
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
        # Funding has been accrued event-by-event as the simulation advanced, so the
        # same settlement is not counted twice at closure.
        funding_pnl = pos.funding_pnl
        slip_cost = (notional_in + notional_out) * slippage_rate
        pnl = gross_pnl + funding_pnl - fees
        risk_amount = pos.risk_amount
        realised_r = pnl / risk_amount if risk_amount > 0 else 0.0
    return ClosedTrade(pos.symbol, pos.side, pos.entry_time, candle.open_time, pos.entry_price, exit_px,
                       exit_reason, pnl, realised_r, risk_amount, pos.candidate_id, config.mode,
                       fees, funding_pnl, slip_cost)

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
