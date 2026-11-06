from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from rslr.config import settings
from rslr.data.candles import build_timeframes
from rslr.exchange.mexc import MEXCRestClient
from rslr.models import Contract, TradeSignal
from rslr.risk import size_position
from rslr.strategy import SignalEngine
from rslr.intelligence import relative_strength


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    module: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry: float
    stop: float
    target: float
    contracts: float
    risk_budget_usd: float
    gross_pnl: float
    fees: float
    slippage: float
    funding: float
    net_pnl: float
    result: str
    holding_hours: float


@dataclass
class _Candidate:
    symbol: str
    signal: TradeSignal
    entry_time: pd.Timestamp
    entry_price: float
    contract: Contract
    funding_history: pd.DataFrame


@dataclass
class _Position:
    symbol: str
    side: str
    module: str
    signal_time: str
    entry_time: pd.Timestamp
    entry: float
    stop: float
    target: float
    contracts: float
    risk_budget: float
    funding_rate: float
    contract: Contract
    fee_rate: float
    slippage_rate: float
    funding_history: pd.DataFrame


@dataclass
class BacktestResult:
    period: str
    start: str
    end: str
    starting_equity: float
    ending_equity: float
    coins_tested: int
    total_signals: int
    trades_taken: int
    long_signals: int
    short_signals: int
    tp_hits: int
    sl_hits: int
    open_unresolved: int
    expired: int
    wins: int
    losses: int
    tp_hit_rate_pct: float | None
    stop_hit_rate_pct: float | None
    net_pnl_usd: float
    net_return_pct: float
    gross_pnl_usd: float
    total_fees_usd: float
    total_slippage_usd: float
    funding_pnl_usd: float
    max_drawdown_pct: float
    profit_factor: float | None
    average_r: float | None
    average_holding_hours: float | None
    sweep_signals: int
    donchian_signals: int
    sweep_net_pnl: float
    donchian_net_pnl: float
    long_net_pnl: float
    short_net_pnl: float
    trades: list[BacktestTrade]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["trades"] = [asdict(t) for t in self.trades]
        return payload


def _trade_from_position(pos: _Position, exit_price: float, exit_time: pd.Timestamp, result: str) -> BacktestTrade:
    duration = max(0.0, (exit_time - pos.entry_time).total_seconds() / 3600)
    direction = 1.0 if pos.side == "LONG" else -1.0
    gross = direction * (exit_price - pos.entry) * pos.contract.contract_size * pos.contracts
    entry_notional = pos.entry * pos.contract.contract_size * pos.contracts
    exit_notional = exit_price * pos.contract.contract_size * pos.contracts
    # Fees/slippage are charged on both entry and exit. Settled historical funding
    # uses the exchange funding-rate sign convention: positive rates charge longs
    # and credit shorts; negative rates do the reverse.
    fees = (entry_notional + exit_notional) * pos.fee_rate
    slippage = (entry_notional + exit_notional) * pos.slippage_rate
    funding_rows = pos.funding_history
    if not funding_rows.empty:
        funding_rows = funding_rows[
            (funding_rows.timestamp > pos.entry_time) & (funding_rows.timestamp <= exit_time)
        ]
    funding_direction = 1.0 if pos.side == "LONG" else -1.0
    funding = entry_notional * sum(float(rate) * funding_direction for rate in funding_rows.funding_rate) if not funding_rows.empty else 0.0
    net = gross - fees - slippage - funding
    return BacktestTrade(
        symbol=pos.symbol,
        side=pos.side,
        module=pos.module,
        signal_time=pos.signal_time,
        entry_time=pos.entry_time.isoformat(),
        exit_time=exit_time.isoformat(),
        entry=pos.entry,
        stop=pos.stop,
        target=pos.target,
        contracts=pos.contracts,
        risk_budget_usd=pos.risk_budget,
        gross_pnl=gross,
        fees=fees,
        slippage=slippage,
        funding=funding,
        net_pnl=net,
        result=result,
        holding_hours=duration,
    )


def _bar_exit(pos: _Position, bar: pd.Series, timestamp: pd.Timestamp) -> tuple[float, str] | None:
    opened = float(bar.open)
    high = float(bar.high)
    low = float(bar.low)
    if pos.side == "LONG":
        if opened <= pos.stop:
            return opened, "SL"  # gap-through-stop receives the worse opening fill
        hit_sl, hit_tp = low <= pos.stop, high >= pos.target
    else:
        if opened >= pos.stop:
            return opened, "SL"
        hit_sl, hit_tp = high >= pos.stop, low <= pos.target
    if hit_sl:  # conservative ordering if both levels are touched in one 1H candle
        return pos.stop, "SL"
    if hit_tp:
        return pos.target, "TP"
    if timestamp >= pos.entry_time + pd.Timedelta(hours=settings.max_hold_hours):
        return float(bar.close), "EXPIRED"
    return None


def _simulate_portfolio(
    candidates_by_entry_time: dict[pd.Timestamp, list[_Candidate]],
    frames: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[BacktestTrade]:
    # Build timestamp-indexed maps for fast cross-asset portfolio replay.
    bar_maps: dict[str, dict[pd.Timestamp, pd.Series]] = {}
    for symbol, frame in frames.items():
        bar_maps[symbol] = {pd.Timestamp(row["timestamp"]): row for _, row in frame.iterrows()}

    timeline = sorted({
        ts for frame in frames.values() for ts in pd.to_datetime(frame.timestamp, utc=True).tolist() if start <= ts < end
    })
    positions: list[_Position] = []
    closed: list[BacktestTrade] = []
    equity = settings.account_equity_usd
    last_exit: dict[str, pd.Timestamp] = {}

    for timestamp in timeline:
        equity_at_open = equity
        active_at_open = len(positions)
        available_slots = max(0, settings.max_open_positions - active_at_open)
        survivors: list[_Position] = []
        # Evaluate positions that existed before this candle.
        for pos in positions:
            bar = bar_maps.get(pos.symbol, {}).get(timestamp)
            if bar is None:
                survivors.append(pos)
                continue
            outcome = _bar_exit(pos, bar, timestamp)
            if outcome:
                trade = _trade_from_position(pos, outcome[0], timestamp, outcome[1])
                closed.append(trade)
                equity += trade.net_pnl
                last_exit[pos.symbol] = timestamp
            else:
                survivors.append(pos)
        positions = survivors

        # Slots are based on what was open at the start of the candle, so a position
        # that stops/targets intrabar cannot create a look-ahead entry at that candle's open.
        candidates = candidates_by_entry_time.get(timestamp, [])
        candidates = sorted(candidates, key=lambda c: abs(c.signal.relative_strength_score), reverse=True)
        opened_this_bar: list[_Position] = []
        for candidate in candidates:
            if available_slots <= 0:
                break
            if any(p.symbol == candidate.symbol for p in positions + opened_this_bar):
                continue
            last = last_exit.get(candidate.symbol)
            if last and timestamp - last < pd.Timedelta(hours=settings.same_symbol_cooldown_hours):
                continue
            signal = candidate.signal
            stop_distance = abs(signal.reference_entry - signal.stop_loss)
            entry = candidate.entry_price
            stop = entry - stop_distance if signal.side == "LONG" else entry + stop_distance
            sizing = size_position(
                side=signal.side,
                entry=entry,
                stop=stop,
                equity=max(equity_at_open, 0.0),
                contract=candidate.contract,
                risk_fraction=settings.risk_per_trade,
                taker_fee_rate=settings.taker_fee_rate,
                slippage_rate=settings.slippage_rate,
                funding_rate=signal.funding_rate,
                leverage=settings.default_leverage,
                max_leverage=settings.max_leverage,
                max_margin_fraction=settings.max_margin_fraction,
            )
            if not sizing.accepted:
                continue
            position = _Position(
                symbol=candidate.symbol,
                side=signal.side,
                module=signal.module,
                signal_time=signal.signal_time.isoformat(),
                entry_time=timestamp,
                entry=entry,
                stop=sizing.stop_loss,
                target=sizing.take_profit,
                contracts=sizing.contracts,
                risk_budget=sizing.risk_amount_usd,
                funding_rate=signal.funding_rate,
                contract=candidate.contract,
                fee_rate=max(settings.taker_fee_rate, candidate.contract.taker_fee_rate),
                slippage_rate=settings.slippage_rate,
                funding_history=candidate.funding_history,
            )
            opened_this_bar.append(position)
            available_slots -= 1

        # Newly entered positions are exposed to the remainder of their entry candle.
        for pos in opened_this_bar:
            bar = bar_maps.get(pos.symbol, {}).get(timestamp)
            if bar is None:
                positions.append(pos)
                continue
            outcome = _bar_exit(pos, bar, timestamp)
            if outcome:
                trade = _trade_from_position(pos, outcome[0], timestamp, outcome[1])
                closed.append(trade)
                equity += trade.net_pnl
                last_exit[pos.symbol] = timestamp
            else:
                positions.append(pos)

    # Remaining open positions are unresolved, not wins/losses, with no fictional PnL.
    for pos in positions:
        closed.append(
            BacktestTrade(
                symbol=pos.symbol,
                side=pos.side,
                module=pos.module,
                signal_time=pos.signal_time,
                entry_time=pos.entry_time.isoformat(),
                exit_time=end.isoformat(),
                entry=pos.entry,
                stop=pos.stop,
                target=pos.target,
                contracts=pos.contracts,
                risk_budget_usd=pos.risk_budget,
                gross_pnl=0.0,
                fees=0.0,
                slippage=0.0,
                funding=0.0,
                net_pnl=0.0,
                result="OPEN",
                holding_hours=max(0.0, (end - pos.entry_time).total_seconds() / 3600),
            )
        )
    return closed


def calculate_metrics(
    period: str, start: datetime, end: datetime, starting_equity: float, trades: list[BacktestTrade], coins: int,
    signals: list[TradeSignal] | None = None,
) -> BacktestResult:
    resolved = [t for t in trades if t.result in {"TP", "SL", "EXPIRED"}]
    winners = [t for t in resolved if t.net_pnl > 0]
    losers = [t for t in resolved if t.net_pnl < 0]
    tp = sum(t.result == "TP" for t in trades)
    sl = sum(t.result == "SL" for t in trades)
    opened = sum(t.result == "OPEN" for t in trades)
    expired = sum(t.result == "EXPIRED" for t in trades)
    net = sum(t.net_pnl for t in resolved)
    gross = sum(t.gross_pnl for t in resolved)
    fees = sum(t.fees for t in trades)
    slippage = sum(t.slippage for t in trades)
    funding = sum(t.funding for t in trades)
    ending = starting_equity + net
    running = peak = starting_equity
    max_dd = 0.0
    for trade in sorted(resolved, key=lambda t: t.exit_time):
        running += trade.net_pnl
        peak = max(peak, running)
        if peak > 0:
            max_dd = max(max_dd, (peak - running) / peak)
    gross_wins = sum(t.net_pnl for t in winners)
    gross_losses = abs(sum(t.net_pnl for t in losers))
    pf = gross_wins / gross_losses if gross_losses > 0 else None
    r_values = [t.net_pnl / t.risk_budget_usd for t in resolved if t.risk_budget_usd > 0]
    sweep = [t for t in trades if "SWEEP" in t.module]
    donchian = [t for t in trades if "DONCHIAN" in t.module]
    longs = [t for t in trades if t.side == "LONG"]
    shorts = [t for t in trades if t.side == "SHORT"]
    resolved_n = tp + sl + expired
    signal_events = signals if signals is not None else []
    signal_count = len(signal_events) if signals is not None else len(trades)
    long_signal_count = sum(s.side == "LONG" for s in signal_events) if signals is not None else sum(t.side == "LONG" for t in trades)
    short_signal_count = sum(s.side == "SHORT" for s in signal_events) if signals is not None else sum(t.side == "SHORT" for t in trades)
    sweep_signal_count = sum("SWEEP" in s.module for s in signal_events) if signals is not None else len(sweep)
    donchian_signal_count = sum("DONCHIAN" in s.module for s in signal_events) if signals is not None else len(donchian)
    return BacktestResult(
        period=period,
        start=start.isoformat(),
        end=end.isoformat(),
        starting_equity=starting_equity,
        ending_equity=ending,
        coins_tested=coins,
        total_signals=signal_count,
        trades_taken=len(trades),
        long_signals=long_signal_count,
        short_signals=short_signal_count,
        tp_hits=tp,
        sl_hits=sl,
        open_unresolved=opened,
        expired=expired,
        wins=len(winners),
        losses=len(losers),
        tp_hit_rate_pct=tp / resolved_n * 100 if resolved_n else None,
        stop_hit_rate_pct=sl / resolved_n * 100 if resolved_n else None,
        net_pnl_usd=net,
        net_return_pct=net / starting_equity * 100 if starting_equity else 0.0,
        gross_pnl_usd=gross,
        total_fees_usd=fees,
        total_slippage_usd=slippage,
        funding_pnl_usd=-funding,
        max_drawdown_pct=max_dd * 100,
        profit_factor=pf,
        average_r=sum(r_values) / len(r_values) if r_values else None,
        average_holding_hours=sum(t.holding_hours for t in resolved) / len(resolved) if resolved else None,
        sweep_signals=sweep_signal_count,
        donchian_signals=donchian_signal_count,
        sweep_net_pnl=sum(t.net_pnl for t in sweep),
        donchian_net_pnl=sum(t.net_pnl for t in donchian),
        long_net_pnl=sum(t.net_pnl for t in longs),
        short_net_pnl=sum(t.net_pnl for t in shorts),
        trades=trades,
    )


def run_backtest(period: str, client: MEXCRestClient | None = None) -> BacktestResult:
    normalized = period.lower().replace("days", "d").replace("day", "d")
    days = {"1d": 1, "7d": 7, "30d": 30}.get(normalized)
    if days is None:
        raise ValueError("Backtest period must be 1D, 7D or 30D")
    owned = client is None
    client = client or MEXCRestClient()
    try:
        end_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=days)
        data_start = start_dt - timedelta(days=42)
        contracts = client.get_universe(settings.universe_size)
        all_contracts = {c.symbol: c for c in client.get_contracts()}
        btc_contract = all_contracts.get("BTC_USDT")
        if btc_contract is None:
            raise RuntimeError("BTC_USDT contract metadata unavailable")
        if all(c.symbol != "BTC_USDT" for c in contracts):
            contracts = [btc_contract, *contracts]
        btc = client.get_klines("BTC_USDT", data_start, end_dt, "Min60")
        if len(btc) < 960:
            raise RuntimeError(f"Insufficient BTC warm-up candles: {len(btc)} returned; need at least 960")
        btc_frames = build_timeframes(btc)
        engine = SignalEngine()
        frames: dict[str, pd.DataFrame] = {}
        candidates: dict[pd.Timestamp, list[_Candidate]] = {}
        generated_signals: list[TradeSignal] = []
        tested = 0
        start_ts, end_ts = pd.Timestamp(start_dt), pd.Timestamp(end_dt)

        contract_map: dict[str, Contract] = {}
        funding_map: dict[str, pd.DataFrame] = {}
        timeframe_cache: dict[str, dict[str, pd.DataFrame]] = {}

        # Collect the same point-in-time data set for all eligible contracts before
        # ranking. This makes the 12H relative-strength ranking cross-sectional.
        for contract in contracts:
            try:
                frame = btc if contract.symbol == "BTC_USDT" else client.get_klines(contract.symbol, data_start, end_dt, "Min60")
                if len(frame) < 960:
                    continue
                funding = client.get_funding_history(contract.symbol, data_start, end_dt)
                if funding.empty:
                    continue
                frames[contract.symbol] = frame
                funding_map[contract.symbol] = funding
                timeframe_cache[contract.symbol] = build_timeframes(frame)
                contract_map[contract.symbol] = contract
            except Exception as exc:
                import logging
                logging.getLogger(__name__).exception("Skipping %s due to backtest data error: %s", contract.symbol, exc)

        if "BTC_USDT" not in frames:
            raise RuntimeError("BTC_USDT candles or funding history are unavailable")
        tested = len(frames)
        rank_cache: dict[pd.Timestamp, tuple[set[str], set[str]]] = {}
        interval_ns = 12 * 60 * 60 * 1_000_000_000

        def ranked_sets(asof: pd.Timestamp) -> tuple[set[str], set[str]]:
            bucket = pd.Timestamp((asof.value // interval_ns) * interval_ns, tz="UTC")
            if bucket in rank_cache:
                return rank_cache[bucket]
            btc_12h = timeframe_cache["BTC_USDT"]["12H"]
            btc_12h = btc_12h[btc_12h.timestamp + pd.Timedelta(hours=12) <= bucket]
            scores: dict[str, float] = {}
            for candidate_symbol, tf in timeframe_cache.items():
                series = tf["12H"]
                series = series[series.timestamp + pd.Timedelta(hours=12) <= bucket]
                score = relative_strength(series, btc_12h)
                if score is not None:
                    scores[candidate_symbol] = score
            if len(scores) < 5:
                value = (set(scores), set(scores))
            else:
                ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
                keep_n = max(1, int(len(ranked) * 0.35 + 0.999999))
                value = (
                    {symbol for symbol, _ in ranked[:keep_n]},
                    {symbol for symbol, _ in ranked[-keep_n:]},
                )
            rank_cache[bucket] = value
            return value

        for symbol, frame in frames.items():
            contract = contract_map[symbol]
            funding = funding_map[symbol]
            symbol_frames = timeframe_cache[symbol]
            for i in range(960, len(frame) - 1):
                signal_time = pd.Timestamp(frame.iloc[i].timestamp)
                if signal_time < start_ts or signal_time >= end_ts:
                    continue
                btc_prefix = btc[btc.timestamp <= signal_time]
                if len(btc_prefix) < 960:
                    continue
                asof = signal_time + pd.Timedelta(hours=1)
                long_ranked, short_ranked = ranked_sets(asof)
                signals = engine.generate_for_symbol(
                    symbol=symbol,
                    symbol_1h=frame.iloc[: i + 1],
                    btc_1h=btc_prefix,
                    funding=funding,
                    contract=contract,
                    equity=settings.account_equity_usd,
                    symbol_frames_cache=symbol_frames,
                    btc_frames_cache=btc_frames,
                )
                if not signals:
                    continue
                entry_index = i + 1
                entry_time = pd.Timestamp(frame.iloc[entry_index].timestamp)
                if entry_time >= end_ts:
                    continue
                for signal in signals:
                    if len(timeframe_cache) >= 5:
                        if signal.side == "LONG" and symbol not in long_ranked:
                            continue
                        if signal.side == "SHORT" and symbol not in short_ranked:
                            continue
                    generated_signals.append(signal)
                    candidates.setdefault(entry_time, []).append(
                        _Candidate(
                            symbol=symbol,
                            signal=signal,
                            entry_time=entry_time,
                            entry_price=float(frame.iloc[entry_index].open),
                            contract=contract,
                            funding_history=funding,
                        )
                    )

        trades = _simulate_portfolio(candidates, frames, start_ts, end_ts)
        return calculate_metrics(
            period.upper(), start_dt, end_dt, settings.account_equity_usd, trades, tested,
            signals=generated_signals,
        )
    finally:
        if owned:
            client.close()
