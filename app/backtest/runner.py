from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_right
from dataclasses import dataclass
from ..analysis.engine import (
    MAX_SETUP_AGE_15M,
    _five_minute_trigger,
    _pullback_retest,
    _bos_events,
    analyze_candles,
    build_btc_context,
    btc_filter_ok,
)
from ..automation.mexc_client import MexcClient
from ..automation.universe import MexcUniverse
from ..config import Settings
from .report import BacktestSummary, summarize
from .simulator import SimulatedTrade, simulate_trade

LOGGER = logging.getLogger(__name__)

MIN_4H_WARMUP_MS = 40 * 24 * 60 * 60 * 1000
MIN_1H_WARMUP_MS = 14 * 24 * 60 * 60 * 1000
MIN_15M_WARMUP_MS = 3 * 24 * 60 * 60 * 1000
MIN_5M_WARMUP_MS = 24 * 60 * 60 * 1000
MIN_1D_WARMUP_MS = 40 * 24 * 60 * 60 * 1000
M5_MS = 300_000
M15_MS = 900_000
M1H_MS = 3_600_000
M4H_MS = 14_400_000
M1D_MS = 86_400_000
MAX_KLINE_POINTS = 2000

INTERVALS = {
    "4h": "Hour4",
    "1h": "Min60",
    "15m": "Min15",
    "5m": "Min5",
    "1d": "Day1",
}


class BacktestAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class SymbolHistory:
    symbol: str
    candles_4h: list[list[float | int]]
    candles_1h: list[list[float | int]]
    candles_15m: list[list[float | int]]
    candles_5m: list[list[float | int]]
    candles_1d: list[list[float | int]]


def _align_5m_timestamp(timestamp_ms: int) -> int:
    return (int(timestamp_ms) // M5_MS) * M5_MS


def _slice_closed(
    rows: list[list[float | int]],
    timeframe_ms: int,
    now_ms: int,
) -> list[list[float | int]]:
    # Rows are already sorted and normalized by MexcClient. A candle is
    # considered closed when its opening time + timeframe <= now_ms.
    cutoff = int(now_ms) - timeframe_ms
    if cutoff < 0:
        return []
    times = [int(row[0]) for row in rows]
    return rows[:bisect_right(times, cutoff)]


async def _fetch_range(
    client: MexcClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[float | int]]:
    """Paginate historical klines without requesting more than 2000 points."""
    interval_ms = {
        "Min5": M5_MS,
        "Min15": M15_MS,
        "Min60": M1H_MS,
        "Hour4": M4H_MS,
        "Day1": M1D_MS,
    }[interval]

    cursor = (int(start_ms) // interval_ms) * interval_ms
    final = int(end_ms)
    result: dict[int, list[float | int]] = {}
    page_span = interval_ms * (MAX_KLINE_POINTS - 1)

    while cursor <= final:
        page_end = min(final, cursor + page_span)
        rows = await client.get_klines_range(
            symbol,
            interval,
            cursor,
            page_end,
            limit=MAX_KLINE_POINTS,
        )
        for row in rows:
            timestamp = int(row[0])
            if cursor <= timestamp <= final:
                result[timestamp] = row

        if not rows:
            cursor = page_end + interval_ms
            continue

        last_timestamp = max(int(row[0]) for row in rows)
        next_cursor = last_timestamp + interval_ms
        if next_cursor <= cursor:
            next_cursor = page_end + interval_ms
        cursor = next_cursor

    return [result[timestamp] for timestamp in sorted(result)]


class BacktestRunner:
    """Run historical technical paper backtests against the live engine code."""

    def __init__(
        self,
        client: MexcClient,
        universe: MexcUniverse,
        settings: Settings,
        *,
        max_concurrency: int = 4,
    ) -> None:
        self.client = client
        self.universe = universe
        self.settings = settings
        self.max_concurrency = max(1, min(int(max_concurrency), 4))
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    async def run(self, days: int) -> BacktestSummary:
        days = int(days)
        if days not in {7, 30, 90}:
            raise ValueError("Supported backtests: 7D, 30D, 90D")

        if self._lock.locked():
            raise BacktestAlreadyRunning("A backtest is already running. Please wait for it to finish.")

        async with self._lock:
            started = time.monotonic()
            now_ms = int(time.time() * 1000)
            period_end = _align_5m_timestamp(now_ms)
            period_start = period_end - days * 24 * 60 * 60 * 1000

            symbols = await self.universe.refresh()
            symbols = list(symbols[:300])
            if not symbols:
                raise RuntimeError("No eligible MEXC Futures symbols are available for backtesting.")

            LOGGER.info(
                "BACKTEST START | days=%d symbols=%d start=%d end=%d",
                days,
                len(symbols),
                period_start,
                period_end,
            )

            btc_history = await self._fetch_btc_history(period_start, period_end)
            trades: list[SimulatedTrade] = []
            errors = 0
            tested = 0

            semaphore = asyncio.Semaphore(self.max_concurrency)

            async def worker(symbol: str) -> tuple[str, list[SimulatedTrade], str | None]:
                async with semaphore:
                    try:
                        history = await self._fetch_symbol_history(
                            symbol,
                            period_start,
                            period_end,
                        )
                        symbol_trades = self._backtest_symbol(
                            history,
                            period_start,
                            period_end,
                            btc_history,
                        )
                        return symbol, symbol_trades, None
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.exception("BACKTEST symbol failed: %s", symbol)
                        return symbol, [], str(exc)

            batch_size = self.max_concurrency
            for offset in range(0, len(symbols), batch_size):
                batch = symbols[offset : offset + batch_size]
                results = await asyncio.gather(*(worker(symbol) for symbol in batch))
                for symbol, symbol_trades, error in results:
                    if error:
                        errors += 1
                        continue
                    tested += 1
                    trades.extend(symbol_trades)
                LOGGER.info(
                    "BACKTEST PROGRESS | days=%d processed=%d/%d tested=%d errors=%d signals=%d",
                    days,
                    min(offset + batch_size, len(symbols)),
                    len(symbols),
                    tested,
                    errors,
                    len(trades),
                )

            trades.sort(key=lambda trade: trade.signal_time_ms)
            summary = summarize(
                days=days,
                coins_selected=len(symbols),
                coins_tested=tested,
                data_errors=errors,
                trades=trades,
            )

            LOGGER.info(
                "BACKTEST COMPLETE | days=%d tested=%d errors=%d signals=%d duration=%.2fs",
                days,
                tested,
                errors,
                len(trades),
                time.monotonic() - started,
            )
            return summary

    async def _fetch_symbol_history(
        self,
        symbol: str,
        period_start: int,
        period_end: int,
    ) -> SymbolHistory:
        c4_start = period_start - MIN_4H_WARMUP_MS
        c1_start = period_start - MIN_1H_WARMUP_MS
        c15_start = period_start - MIN_15M_WARMUP_MS
        c5_start = period_start - MIN_5M_WARMUP_MS
        c1d_start = period_start - MIN_1D_WARMUP_MS

        c4h, c1h, c15m, c5m, c1d = await asyncio.gather(
            _fetch_range(self.client, symbol, INTERVALS["4h"], c4_start, period_end),
            _fetch_range(self.client, symbol, INTERVALS["1h"], c1_start, period_end),
            _fetch_range(self.client, symbol, INTERVALS["15m"], c15_start, period_end),
            _fetch_range(self.client, symbol, INTERVALS["5m"], c5_start, period_end),
            _fetch_range(self.client, symbol, INTERVALS["1d"], c1d_start, period_end),
        )

        return SymbolHistory(
            symbol=symbol,
            candles_4h=c4h,
            candles_1h=c1h,
            candles_15m=c15m,
            candles_5m=c5m,
            candles_1d=c1d,
        )

    async def _fetch_btc_history(
        self,
        period_start: int,
        period_end: int,
    ) -> SymbolHistory:
        symbol = "BTC_USDT"
        return await self._fetch_symbol_history(symbol, period_start, period_end)

    def _backtest_symbol(
        self,
        history: SymbolHistory,
        period_start: int,
        period_end: int,
        btc_history: SymbolHistory,
    ) -> list[SimulatedTrade]:
        c4 = history.candles_4h
        c1 = history.candles_1h
        c15 = history.candles_15m
        c5 = history.candles_5m
        c1d = history.candles_1d

        if len(c4) < 205 or len(c1) < 205 or len(c15) < 80 or len(c5) < 30:
            raise ValueError(
                f"{history.symbol}: insufficient historical candles "
                f"4H={len(c4)} 1H={len(c1)} 15M={len(c15)} 5M={len(c5)}"
            )

        candidate_times: set[int] = set()
        c5_times = [int(row[0]) for row in c5]
        c15_times = [int(row[0]) for row in c15]

        # Generate candidate 5M trigger timestamps only from historically
        # confirmed 15M BOS + retest windows. The final decision still goes
        # through the complete engine at the exact historical timestamp.
        for side in ("LONG", "SHORT"):
            for bos in _bos_events(c15, side, lookback=len(c15)):
                retest = _pullback_retest(c15, side, bos, MAX_SETUP_AGE_15M)
                if not retest.get("valid"):
                    continue

                retest_time = int(retest["time"])
                bos_time = int(bos["time"])
                if retest_time + M15_MS < period_start:
                    continue
                if retest_time > period_end:
                    continue
                if bos_time > retest_time:
                    continue

                trigger_start = max(
                    period_start,
                    retest_time + M15_MS,
                )
                trigger_end = min(
                    period_end - M5_MS,
                    retest_time + 30 * 60 * 1000,
                )
                if trigger_start > trigger_end:
                    continue

                first_index = bisect_right(
                    c5_times,
                    trigger_start - 1,
                )
                last_index = bisect_right(
                    c5_times,
                    trigger_end,
                )

                setup_level = float(bos["level"])
                for index in range(first_index, last_index):
                    trigger_open = int(c5[index][0])
                    trigger_close = trigger_open + M5_MS
                    if trigger_close > period_end:
                        continue
                    try:
                        trigger = _five_minute_trigger(
                            c5[: index + 1],
                            side,
                            setup_level,
                        )
                    except Exception:
                        continue
                    if trigger.get("ready"):
                        candidate_times.add(trigger_close)

        if not candidate_times:
            return []

        c4_times = [int(row[0]) for row in c4]
        c1_times = [int(row[0]) for row in c1]
        c15_times = [int(row[0]) for row in c15]
        c5_times = [int(row[0]) for row in c5]
        c1d_times = [int(row[0]) for row in c1d]
        btc4_times = [int(row[0]) for row in btc_history.candles_4h]
        btc1_times = [int(row[0]) for row in btc_history.candles_1h]
        btc15_times = [int(row[0]) for row in btc_history.candles_15m]

        trades: list[SimulatedTrade] = []
        for signal_close_time in sorted(candidate_times):
            if signal_close_time < period_start or signal_close_time > period_end:
                continue

            # Skip a new signal while a prior paper trade for this symbol is
            # still unresolved. Once resolved, continue after its exit candle.
            if trades and trades[-1].exit_time_ms is None:
                continue
            if trades and trades[-1].exit_time_ms is not None and signal_close_time <= trades[-1].exit_time_ms:
                continue

            c4_slice = c4[:bisect_right(c4_times, signal_close_time - M4H_MS)]
            c1_slice = c1[:bisect_right(c1_times, signal_close_time - M1H_MS)]
            c15_slice = c15[:bisect_right(c15_times, signal_close_time - M15_MS)]
            c5_slice = c5[:bisect_right(c5_times, signal_close_time - M5_MS)]
            c1d_slice = c1d[:bisect_right(c1d_times, signal_close_time - M1D_MS)]

            try:
                analysis = analyze_candles(
                    history.symbol,
                    c4_slice,
                    c1_slice,
                    c15_slice,
                    c5_slice,
                    c1d_slice,
                    now_ms=signal_close_time,
                )
            except Exception:
                continue

            if not analysis.get("technical_candidate"):
                continue

            side = str(analysis.get("setup") or "").upper()
            if side not in {"LONG", "SHORT"}:
                continue

            if history.symbol.upper() == "BTC_USDT":
                btc_ok = True
            else:
                btc_c4 = btc_history.candles_4h[:bisect_right(btc4_times, signal_close_time - M4H_MS)]
                btc_c1 = btc_history.candles_1h[:bisect_right(btc1_times, signal_close_time - M1H_MS)]
                btc_c15 = btc_history.candles_15m[:bisect_right(btc15_times, signal_close_time - M15_MS)]
                try:
                    btc_context = build_btc_context(btc_c4, btc_c1, btc_c15)
                    btc_ok, _ = btc_filter_ok(side, btc_context, is_btc=False)
                except Exception:
                    btc_ok = False

            if not btc_ok:
                continue

            future_start = bisect_right(c5_times, signal_close_time - 1)
            future_candles = c5[future_start:]
            trade = simulate_trade(
                analysis,
                future_candles,
                signal_close_time_ms=signal_close_time,
            )
            if trade is None:
                continue
            trades.append(trade)

        return trades
