from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_right
from dataclasses import dataclass

from ..analysis.engine import (
    MAX_SETUP_AGE_15M,
    MIN_TRIGGER_BODY,
    MIN_TRIGGER_RVOL,
    _five_minute_trigger,
    _pullback_retest,
    _bos_events,
    analyze_candles,
    build_btc_context,
    btc_filter_ok,
    convert_candles,
)
from ..automation.mexc_client import MexcClient
from ..automation.universe import MexcUniverse
from ..config import Settings
from .report import BacktestSummary, summarize
from .simulator import SimulatedTrade, simulate_trade


LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Historical warmups
# ---------------------------------------------------------------------------

MIN_4H_WARMUP_MS = 40 * 24 * 60 * 60 * 1000
MIN_1H_WARMUP_MS = 14 * 24 * 60 * 60 * 1000
MIN_15M_WARMUP_MS = 3 * 24 * 60 * 60 * 1000
MIN_5M_WARMUP_MS = 24 * 60 * 60 * 1000
MIN_1D_WARMUP_MS = 40 * 24 * 60 * 60 * 1000

# ---------------------------------------------------------------------------
# Timeframe sizes
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Backtest execution safety
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 30
MAX_SYMBOL_CONCURRENCY = 2
SYMBOL_FETCH_TIMEOUT_SECONDS = 120
UNIVERSE_REFRESH_TIMEOUT_SECONDS = 60

# The old implementation could spend several minutes inside _backtest_symbol.
# The optimized implementation below avoids that pathological workload.
ANALYSIS_TIMEOUT_SECONDS = 90

HEARTBEAT_INTERVAL_SECONDS = 30

# Keep a reference to the real engine trigger. Tests can monkeypatch the
# imported _five_minute_trigger; in that case we deliberately use the
# original compatibility path so existing integration tests remain valid.
_ENGINE_FIVE_MINUTE_TRIGGER = _five_minute_trigger


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _align_5m_timestamp(timestamp_ms: int) -> int:
    return (int(timestamp_ms) // M5_MS) * M5_MS


def _valid_candle(row: object) -> bool:
    if not isinstance(row, (list, tuple)) or len(row) < 6:
        return False
    try:
        timestamp = int(float(row[0]))
        float(row[1])
        float(row[2])
        float(row[3])
        float(row[4])
        float(row[5])
    except (TypeError, ValueError, IndexError):
        return False
    return timestamp >= 0


def _normalize_rows(
    rows: list[list[float | int]],
    start_ms: int,
    end_ms: int,
) -> list[list[float | int]]:
    result: dict[int, list[float | int]] = {}
    for row in rows:
        if not _valid_candle(row):
            continue
        timestamp = int(float(row[0]))
        if start_ms <= timestamp <= end_ms:
            result[timestamp] = list(row)
    return [result[t] for t in sorted(result)]


def _row_time(row: object) -> int:
    if isinstance(row, dict):
        return int(row["time"])
    return int(float(row[0]))


def _closed_slice(rows: list, timeframe_ms: int, now_ms: int) -> list:
    if not rows:
        return []
    cutoff = int(now_ms) - int(timeframe_ms)
    end_index = bisect_right(rows, cutoff, key=_row_time)
    return rows[:end_index]


def _convert_for_engine(
    rows: list[list[float | int]],
    symbol: str,
    timeframe: str,
) -> list:
    converted = convert_candles(rows)
    if rows and not converted:
        raise ValueError(
            f"{symbol}: {timeframe} candle conversion produced 0 valid rows "
            f"from {len(rows)} raw rows"
        )
    return converted


# ---------------------------------------------------------------------------
# Historical pagination
# ---------------------------------------------------------------------------


async def _fetch_range(
    client: MexcClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[float | int]]:
    interval_sizes = {
        "Min5": M5_MS,
        "Min15": M15_MS,
        "Min60": M1H_MS,
        "Hour4": M4H_MS,
        "Day1": M1D_MS,
    }

    if interval not in interval_sizes:
        raise ValueError(f"Unsupported backtest interval: {interval}")

    interval_ms = interval_sizes[interval]
    start_ms = int(start_ms)
    end_ms = int(end_ms)

    if end_ms < start_ms:
        return []

    cursor = (start_ms // interval_ms) * interval_ms
    final = (end_ms // interval_ms) * interval_ms
    page_span = interval_ms * (MAX_KLINE_POINTS - 1)

    result: dict[int, list[float | int]] = {}
    request_count = 0

    while cursor <= final:
        page_end = min(final, cursor + page_span)
        request_count += 1

        try:
            rows = await asyncio.wait_for(
                client.get_klines_range(
                    symbol,
                    interval,
                    cursor,
                    page_end,
                    limit=MAX_KLINE_POINTS,
                ),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"{symbol} {interval}: MEXC range request timed out after "
                f"{REQUEST_TIMEOUT_SECONDS}s (start={cursor}, end={page_end})"
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"{symbol} {interval}: MEXC range request failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(
                f"{symbol} {interval}: invalid MEXC kline response type "
                f"{type(rows).__name__}"
            )

        valid_timestamps: list[int] = []

        for row in rows:
            if not _valid_candle(row):
                continue
            timestamp = int(float(row[0]))
            if cursor <= timestamp <= page_end:
                result[timestamp] = list(row)
                valid_timestamps.append(timestamp)

        if not rows or not valid_timestamps:
            cursor = page_end + interval_ms
            continue

        last_timestamp = max(valid_timestamps)
        next_cursor = last_timestamp + interval_ms
        if next_cursor <= cursor:
            next_cursor = page_end + interval_ms
        cursor = next_cursor

    final_rows = [result[t] for t in sorted(result)]

    LOGGER.debug(
        "BACKTEST DATA | %s | %s | requests=%d candles=%d",
        symbol,
        interval,
        request_count,
        len(final_rows),
    )
    return final_rows


async def _fetch_timeframe(
    client: MexcClient,
    symbol: str,
    timeframe: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[float | int]]:
    started = time.monotonic()

    LOGGER.info(
        "BACKTEST TF FETCH START | symbol=%s timeframe=%s start=%d end=%d",
        symbol,
        timeframe,
        start_ms,
        end_ms,
    )

    rows = await _fetch_range(
        client,
        symbol,
        interval,
        start_ms,
        end_ms,
    )

    LOGGER.info(
        "BACKTEST TF FETCH DONE | symbol=%s timeframe=%s candles=%d seconds=%.2f",
        symbol,
        timeframe,
        len(rows),
        time.monotonic() - started,
    )
    return rows


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BacktestRunner:
    """
    Historical technical paper backtest.

    No live orders are placed.

    Historical OHLCV can reproduce the deterministic technical engine and
    historical BTC filter, but cannot recreate live-only orderbook, spread,
    ticker-freshness, funding/deals and execution-quality checks.
    """

    def __init__(
        self,
        client: MexcClient,
        universe: MexcUniverse,
        settings: Settings,
        *,
        max_concurrency: int = MAX_SYMBOL_CONCURRENCY,
    ) -> None:
        self.client = client
        self.universe = universe
        self.settings = settings
        self.max_concurrency = max(
            1,
            min(int(max_concurrency), MAX_SYMBOL_CONCURRENCY),
        )
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    # -----------------------------------------------------------------------
    # Heartbeat
    # -----------------------------------------------------------------------

    async def _heartbeat(
        self,
        *,
        days: int,
        state: dict[str, object],
        started: float,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                last_activity = float(state.get("last_activity", started))
                LOGGER.info(
                    "BACKTEST HEARTBEAT | days=%d phase=%s "
                    "processed=%d/%d tested=%d errors=%d signals=%d "
                    "worker=%s symbol=%s idle=%.1fs elapsed=%.1fs",
                    days,
                    str(state.get("phase", "UNKNOWN")),
                    int(state.get("processed", 0)),
                    int(state.get("total", 0)),
                    int(state.get("tested", 0)),
                    int(state.get("errors", 0)),
                    int(state.get("signals", 0)),
                    str(state.get("worker", "-")),
                    str(state.get("current_symbol", "-")),
                    max(0.0, now - last_activity),
                    now - started,
                )

    # -----------------------------------------------------------------------
    # Main backtest
    # -----------------------------------------------------------------------

    async def run(self, days: int) -> BacktestSummary:
        days = int(days)

        if days not in {7, 30, 90}:
            raise ValueError("Supported backtests: 7D, 30D, 90D")

        if self._lock.locked():
            raise BacktestAlreadyRunning(
                "A backtest is already running. Please wait for it to finish."
            )

        async with self._lock:
            started = time.monotonic()
            heartbeat_stop = asyncio.Event()
            heartbeat_task: asyncio.Task | None = None
            workers: list[asyncio.Task] = []

            state: dict[str, object] = {
                "phase": "INITIALIZING",
                "total": 0,
                "processed": 0,
                "tested": 0,
                "errors": 0,
                "signals": 0,
                "worker": "-",
                "current_symbol": "-",
                "last_activity": started,
            }

            def touch(
                *,
                phase: str | None = None,
                symbol: str | None = None,
                worker: int | str | None = None,
            ) -> None:
                if phase is not None:
                    state["phase"] = phase
                if symbol is not None:
                    state["current_symbol"] = symbol
                if worker is not None:
                    state["worker"] = worker
                state["last_activity"] = time.monotonic()

            heartbeat_task = asyncio.create_task(
                self._heartbeat(
                    days=days,
                    state=state,
                    started=started,
                    stop_event=heartbeat_stop,
                ),
                name="backtest-heartbeat",
            )

            trades: list[SimulatedTrade] = []

            try:
                now_ms = int(time.time() * 1000)
                period_end = _align_5m_timestamp(now_ms)
                period_start = period_end - days * 24 * 60 * 60 * 1000

                LOGGER.info(
                    "BACKTEST INIT | days=%d period_start=%d period_end=%d",
                    days,
                    period_start,
                    period_end,
                )

                # -----------------------------------------------------------
                # Universe
                # -----------------------------------------------------------

                touch(phase="UNIVERSE", symbol="-", worker="-")
                LOGGER.info("BACKTEST UNIVERSE FETCH START | days=%d", days)

                try:
                    symbols = await asyncio.wait_for(
                        self.universe.refresh(),
                        timeout=UNIVERSE_REFRESH_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    raise TimeoutError(
                        "MEXC backtest universe refresh timed out after "
                        f"{UNIVERSE_REFRESH_TIMEOUT_SECONDS}s"
                    ) from exc

                symbols = list(symbols or [])[:300]
                if not symbols:
                    raise RuntimeError(
                        "No eligible MEXC Futures symbols are available for backtesting."
                    )

                state["total"] = len(symbols)

                LOGGER.info(
                    "BACKTEST START | days=%d symbols=%d start=%d end=%d",
                    days,
                    len(symbols),
                    period_start,
                    period_end,
                )
                LOGGER.info(
                    "BACKTEST UNIVERSE READY | symbols=%d concurrency=%d "
                    "request_timeout=%ss symbol_timeout=%ss analysis_timeout=%ss",
                    len(symbols),
                    self.max_concurrency,
                    REQUEST_TIMEOUT_SECONDS,
                    SYMBOL_FETCH_TIMEOUT_SECONDS,
                    ANALYSIS_TIMEOUT_SECONDS,
                )

                # -----------------------------------------------------------
                # BTC history
                # -----------------------------------------------------------

                touch(phase="BTC DATA", symbol="BTC_USDT", worker="-")
                LOGGER.info("BACKTEST BTC FETCH START | days=%d", days)

                btc_history = await asyncio.wait_for(
                    self._fetch_btc_history(period_start, period_end),
                    timeout=SYMBOL_FETCH_TIMEOUT_SECONDS,
                )

                LOGGER.info(
                    "BACKTEST BTC FETCH DONE | 4H=%d 1H=%d 15M=%d 5M=%d 1D=%d",
                    len(btc_history.candles_4h),
                    len(btc_history.candles_1h),
                    len(btc_history.candles_15m),
                    len(btc_history.candles_5m),
                    len(btc_history.candles_1d),
                )

                btc_engine_cache = (
                    _convert_for_engine(
                        btc_history.candles_4h, "BTC_USDT", "4H"
                    ),
                    _convert_for_engine(
                        btc_history.candles_1h, "BTC_USDT", "1H"
                    ),
                    _convert_for_engine(
                        btc_history.candles_15m, "BTC_USDT", "15M"
                    ),
                )

                if (
                    len(btc_engine_cache[0]) < 205
                    or len(btc_engine_cache[1]) < 205
                    or len(btc_engine_cache[2]) < 80
                ):
                    raise ValueError(
                        "BTC historical context is insufficient for the deterministic engine"
                    )

                # -----------------------------------------------------------
                # Rolling workers
                # -----------------------------------------------------------

                touch(phase="SYMBOL DATA", symbol="-", worker="-")
                queue: asyncio.Queue[str | None] = asyncio.Queue()

                for symbol in symbols:
                    queue.put_nowait(symbol)
                for _ in range(self.max_concurrency):
                    queue.put_nowait(None)

                state_lock = asyncio.Lock()

                async def worker(worker_id: int) -> None:
                    while True:
                        symbol = await queue.get()
                        try:
                            if symbol is None:
                                return

                            touch(
                                phase="SYMBOL DATA",
                                symbol=symbol,
                                worker=worker_id,
                            )

                            fetch_started = time.monotonic()
                            LOGGER.info(
                                "BACKTEST FETCH START | worker=%d symbol=%s",
                                worker_id,
                                symbol,
                            )

                            try:
                                history = await asyncio.wait_for(
                                    self._fetch_symbol_history(
                                        symbol,
                                        period_start,
                                        period_end,
                                    ),
                                    timeout=SYMBOL_FETCH_TIMEOUT_SECONDS,
                                )

                                LOGGER.info(
                                    "BACKTEST FETCH DONE | worker=%d symbol=%s | "
                                    "4H=%d 1H=%d 15M=%d 5M=%d 1D=%d | seconds=%.2f",
                                    worker_id,
                                    symbol,
                                    len(history.candles_4h),
                                    len(history.candles_1h),
                                    len(history.candles_15m),
                                    len(history.candles_5m),
                                    len(history.candles_1d),
                                    time.monotonic() - fetch_started,
                                )

                                touch(
                                    phase="ANALYSIS",
                                    symbol=symbol,
                                    worker=worker_id,
                                )
                                LOGGER.info(
                                    "BACKTEST ANALYSIS START | worker=%d symbol=%s",
                                    worker_id,
                                    symbol,
                                )

                                analysis_started = time.monotonic()

                                symbol_trades = await asyncio.wait_for(
                                    asyncio.to_thread(
                                        self._backtest_symbol,
                                        history,
                                        period_start,
                                        period_end,
                                        btc_history,
                                        btc_engine_cache,
                                    ),
                                    timeout=ANALYSIS_TIMEOUT_SECONDS,
                                )

                                LOGGER.info(
                                    "BACKTEST ANALYSIS DONE | worker=%d symbol=%s "
                                    "| signals=%d seconds=%.2f",
                                    worker_id,
                                    symbol,
                                    len(symbol_trades),
                                    time.monotonic() - analysis_started,
                                )

                                trades.extend(symbol_trades)

                                async with state_lock:
                                    state["tested"] = int(state["tested"]) + 1
                                    state["signals"] = len(trades)

                            except asyncio.CancelledError:
                                LOGGER.warning(
                                    "BACKTEST SYMBOL CANCELLED | worker=%d symbol=%s",
                                    worker_id,
                                    symbol,
                                )
                                raise

                            except Exception as exc:
                                async with state_lock:
                                    state["errors"] = int(state["errors"]) + 1

                                LOGGER.exception(
                                    "BACKTEST symbol failed: %s | %s",
                                    symbol,
                                    exc,
                                )
                                LOGGER.error(
                                    "BACKTEST DATA ERROR | %s | %s: %s",
                                    symbol,
                                    type(exc).__name__,
                                    exc,
                                )

                            finally:
                                async with state_lock:
                                    state["processed"] = int(state["processed"]) + 1
                                    processed = int(state["processed"])
                                    tested = int(state["tested"])
                                    errors = int(state["errors"])
                                    signal_count = int(state["signals"])

                                touch(
                                    phase="SYMBOL DATA",
                                    symbol=symbol,
                                    worker=worker_id,
                                )

                                LOGGER.info(
                                    "BACKTEST SYMBOL COMPLETE | processed=%d/%d "
                                    "| tested=%d | errors=%d | signals=%d | symbol=%s",
                                    processed,
                                    len(symbols),
                                    tested,
                                    errors,
                                    signal_count,
                                    symbol,
                                )
                                LOGGER.info(
                                    "BACKTEST PROGRESS | days=%d processed=%d/%d "
                                    "tested=%d errors=%d signals=%d",
                                    days,
                                    processed,
                                    len(symbols),
                                    tested,
                                    errors,
                                    signal_count,
                                )

                                await asyncio.sleep(0)

                        finally:
                            queue.task_done()

                workers = [
                    asyncio.create_task(
                        worker(worker_id),
                        name=f"backtest-worker-{worker_id}",
                    )
                    for worker_id in range(self.max_concurrency)
                ]

                try:
                    await asyncio.gather(*workers)
                except BaseException:
                    for task in workers:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    raise

                # -----------------------------------------------------------
                # Final report
                # -----------------------------------------------------------

                touch(phase="FINALIZING", symbol="-", worker="-")
                trades.sort(key=lambda trade: trade.signal_time_ms)

                summary = summarize(
                    days=days,
                    coins_selected=len(symbols),
                    coins_tested=int(state["tested"]),
                    data_errors=int(state["errors"]),
                    trades=trades,
                )

                LOGGER.info(
                    "BACKTEST COMPLETE | days=%d tested=%d errors=%d signals=%d duration=%.2fs",
                    days,
                    int(state["tested"]),
                    int(state["errors"]),
                    len(trades),
                    time.monotonic() - started,
                )
                return summary

            except asyncio.CancelledError:
                LOGGER.warning(
                    "BACKTEST CANCELLED | days=%d duration=%.2fs",
                    days,
                    time.monotonic() - started,
                )
                raise

            finally:
                for task in workers:
                    if not task.done():
                        task.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)

                heartbeat_stop.set()
                if heartbeat_task is not None:
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

    # -----------------------------------------------------------------------
    # Historical data
    # -----------------------------------------------------------------------

    async def _fetch_symbol_history(
        self,
        symbol: str,
        period_start: int,
        period_end: int,
    ) -> SymbolHistory:
        starts = {
            "4h": period_start - MIN_4H_WARMUP_MS,
            "1h": period_start - MIN_1H_WARMUP_MS,
            "15m": period_start - MIN_15M_WARMUP_MS,
            "5m": period_start - MIN_5M_WARMUP_MS,
            "1d": period_start - MIN_1D_WARMUP_MS,
        }

        tasks = [
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "4H",
                    INTERVALS["4h"],
                    starts["4h"],
                    period_end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "1H",
                    INTERVALS["1h"],
                    starts["1h"],
                    period_end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "15M",
                    INTERVALS["15m"],
                    starts["15m"],
                    period_end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "5M",
                    INTERVALS["5m"],
                    starts["5m"],
                    period_end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "1D",
                    INTERVALS["1d"],
                    starts["1d"],
                    period_end,
                )
            ),
        ]

        try:
            c4h, c1h, c15m, c5m, c1d = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception as exc:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise RuntimeError(
                f"{symbol}: historical data fetch failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

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
        return await self._fetch_symbol_history(
            "BTC_USDT",
            period_start,
            period_end,
        )

    # -----------------------------------------------------------------------
    # Optimized 5M trigger metrics
    # -----------------------------------------------------------------------

    @staticmethod
    def _precompute_5m_metrics(
        candles: list,
    ) -> tuple[list[float], list[float], list[float]]:
        """
        Calculate the exact final RSI, RVOL and ATR values needed by
        _five_minute_trigger() for every 5M candle.

        This changes the workload from repeated O(n) indicator calculation
        for every candidate to one O(n) preprocessing pass per symbol.
        """
        count = len(candles)
        rsi_values = [50.0] * count
        rvol_values = [0.0] * count
        atr_values = [0.0] * count

        if count < 2:
            return rsi_values, rvol_values, atr_values

        closes = [float(c["close"]) for c in candles]
        volumes = [float(c["volume"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]

        # Same RSI implementation as indicators.rsi(period=14).
        period = 14
        gains = [0.0] * (count - 1)
        losses = [0.0] * (count - 1)

        for i in range(1, count):
            change = closes[i] - closes[i - 1]
            gains[i - 1] = max(change, 0.0)
            losses[i - 1] = max(-change, 0.0)

        if count >= period + 1:
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period

            def rsi_value() -> float:
                if avg_loss == 0:
                    return 100.0
                rs = avg_gain / avg_loss
                return 100.0 - (100.0 / (1.0 + rs))

            rsi_values[period] = rsi_value()

            for i in range(period + 1, count):
                avg_gain = (
                    (avg_gain * (period - 1)) + gains[i - 1]
                ) / period
                avg_loss = (
                    (avg_loss * (period - 1)) + losses[i - 1]
                ) / period
                rsi_values[i] = rsi_value()

        # Same _relative_volume(candles, lookback=20).
        lookback = 20
        if count >= lookback + 1:
            rolling = sum(volumes[:lookback])
            for i in range(lookback, count):
                average = rolling / lookback
                rvol_values[i] = (
                    volumes[i] / average if average > 0 else 0.0
                )
                rolling += volumes[i]
                rolling -= volumes[i - lookback]

        # Same indicators.atr(candles, period=14).
        atr_period = 14
        true_ranges = [0.0] * count

        for i in range(1, count):
            previous_close = closes[i - 1]
            true_ranges[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - previous_close),
                abs(lows[i] - previous_close),
            )

        if count >= atr_period + 1:
            rolling_tr = sum(true_ranges[1 : atr_period + 1])
            atr_values[atr_period] = rolling_tr / atr_period

            for i in range(atr_period + 1, count):
                rolling_tr += true_ranges[i]
                rolling_tr -= true_ranges[i - atr_period]
                atr_values[i] = rolling_tr / atr_period

        return rsi_values, rvol_values, atr_values

    @staticmethod
    def _fast_trigger_ready(
        candles: list,
        index: int,
        side: str,
        setup_level: float,
        rsi_values: list[float],
        rvol_values: list[float],
    ) -> bool:
        """
        Exact boolean equivalent of _five_minute_trigger().ready for the
        production engine, without allocating c5[:index+1].
        """
        if index < 29:
            return False

        cur = candles[index]
        prev = candles[index - 1]

        open_price = float(cur["open"])
        high = float(cur["high"])
        low = float(cur["low"])
        close = float(cur["close"])

        previous_high = float(prev["high"])
        previous_low = float(prev["low"])

        rng = max(high - low, 1e-12)
        body = abs(close - open_price) / rng

        rsi_value = rsi_values[index]
        rvol_value = rvol_values[index]

        long_level = close > setup_level
        short_level = close < setup_level

        breakout_long = (
            close > open_price
            and close > previous_high
            and long_level
        )
        breakout_short = (
            close < open_price
            and close < previous_low
            and short_level
        )

        reclaim_long = (
            close > open_price
            and long_level
            and low <= setup_level
        )
        reclaim_short = (
            close < open_price
            and short_level
            and high >= setup_level
        )

        momentum_long = (
            rsi_value >= 51.0
            and rvol_value >= MIN_TRIGGER_RVOL
            and body >= MIN_TRIGGER_BODY
        )
        momentum_short = (
            rsi_value <= 49.0
            and rvol_value >= MIN_TRIGGER_RVOL
            and body >= MIN_TRIGGER_BODY
        )

        if side == "LONG":
            return (breakout_long or reclaim_long) and momentum_long

        if side == "SHORT":
            return (breakout_short or reclaim_short) and momentum_short

        return False

    # -----------------------------------------------------------------------
    # Symbol backtest
    # -----------------------------------------------------------------------

    def _backtest_symbol(
        self,
        history: SymbolHistory,
        period_start: int,
        period_end: int,
        btc_history: SymbolHistory,
        btc_engine: tuple[list, list, list] | None = None,
    ) -> list[SimulatedTrade]:
        raw_c4 = history.candles_4h
        raw_c1 = history.candles_1h
        raw_c15 = history.candles_15m
        raw_c5 = history.candles_5m
        raw_c1d = history.candles_1d

        c4 = _convert_for_engine(raw_c4, history.symbol, "4H")
        c1 = _convert_for_engine(raw_c1, history.symbol, "1H")
        c15 = _convert_for_engine(raw_c15, history.symbol, "15M")
        c5 = _convert_for_engine(raw_c5, history.symbol, "5M")
        c1d = _convert_for_engine(raw_c1d, history.symbol, "1D")

        if btc_engine is None:
            btc_c4_engine = _convert_for_engine(
                btc_history.candles_4h, "BTC_USDT", "4H"
            )
            btc_c1_engine = _convert_for_engine(
                btc_history.candles_1h, "BTC_USDT", "1H"
            )
            btc_c15_engine = _convert_for_engine(
                btc_history.candles_15m, "BTC_USDT", "15M"
            )
        else:
            btc_c4_engine, btc_c1_engine, btc_c15_engine = btc_engine

        if len(c4) < 205:
            raise ValueError(
                f"{history.symbol}: insufficient 4H candles ({len(c4)} < 205)"
            )
        if len(c1) < 205:
            raise ValueError(
                f"{history.symbol}: insufficient 1H candles ({len(c1)} < 205)"
            )
        if len(c15) < 80:
            raise ValueError(
                f"{history.symbol}: insufficient 15M candles ({len(c15)} < 80)"
            )
        if len(c5) < 30:
            raise ValueError(
                f"{history.symbol}: insufficient 5M candles ({len(c5)} < 30)"
            )

        c4_times = [_row_time(row) for row in c4]
        c1_times = [_row_time(row) for row in c1]
        c15_times = [_row_time(row) for row in c15]
        c5_times = [_row_time(row) for row in c5]
        c1d_times = [_row_time(row) for row in c1d]

        btc4_times = [_row_time(row) for row in btc_c4_engine]
        btc1_times = [_row_time(row) for row in btc_c1_engine]
        btc15_times = [_row_time(row) for row in btc_c15_engine]

        raw_c5_times = [int(float(row[0])) for row in raw_c5]

        # Precompute 5M indicators once. This is the primary performance fix.
        rsi_values, rvol_values, _atr_values = self._precompute_5m_metrics(c5)

        candidate_times: set[int] = set()

        for side in ("LONG", "SHORT"):
            try:
                bos_events = _bos_events(
                    c15,
                    side,
                    lookback=len(c15),
                )
            except Exception:
                LOGGER.exception(
                    "BACKTEST BOS failed | %s | side=%s",
                    history.symbol,
                    side,
                )
                continue

            for bos in bos_events:
                try:
                    retest = _pullback_retest(
                        c15,
                        side,
                        bos,
                        MAX_SETUP_AGE_15M,
                    )
                except Exception:
                    LOGGER.exception(
                        "BACKTEST RETEST failed | %s | side=%s",
                        history.symbol,
                        side,
                    )
                    continue

                if not retest.get("valid"):
                    continue

                try:
                    retest_time = int(retest["time"])
                    bos_time = int(bos["time"])
                    setup_level = float(bos["level"])
                except (TypeError, ValueError, KeyError):
                    continue

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

                first_index = bisect_right(c5_times, trigger_start - 1)
                last_index = bisect_right(c5_times, trigger_end)

                for index in range(first_index, last_index):
                    trigger_open = c5_times[index]
                    trigger_close = trigger_open + M5_MS

                    if trigger_close > period_end:
                        continue
                    if index < 29:
                        continue

                    # Compatibility path for monkeypatched/custom trigger
                    # functions. Production uses the optimized exact boolean
                    # equivalent above.
                    if _five_minute_trigger is not _ENGINE_FIVE_MINUTE_TRIGGER:
                        try:
                            trigger = _five_minute_trigger(
                                c5[: index + 1],
                                side,
                                setup_level,
                            )
                        except Exception:
                            LOGGER.exception(
                                "BACKTEST TRIGGER failed | %s | side=%s | index=%d",
                                history.symbol,
                                side,
                                index,
                            )
                            continue

                        if trigger.get("ready"):
                            candidate_times.add(trigger_close)
                    else:
                        if self._fast_trigger_ready(
                            c5,
                            index,
                            side,
                            setup_level,
                            rsi_values,
                            rvol_values,
                        ):
                            candidate_times.add(trigger_close)

        if not candidate_times:
            return []

        # ---------------------------------------------------------------
        # Full deterministic engine + paper simulation
        # ---------------------------------------------------------------

        trades: list[SimulatedTrade] = []

        for signal_close_time in sorted(candidate_times):
            if signal_close_time < period_start or signal_close_time > period_end:
                continue

            if trades:
                previous_trade = trades[-1]
                if previous_trade.exit_time_ms is None:
                    continue
                if signal_close_time <= previous_trade.exit_time_ms:
                    continue

            c4_slice = _closed_slice(c4, M4H_MS, signal_close_time)
            c1_slice = _closed_slice(c1, M1H_MS, signal_close_time)
            c15_slice = _closed_slice(c15, M15_MS, signal_close_time)
            c5_slice = _closed_slice(c5, M5_MS, signal_close_time)
            c1d_slice = _closed_slice(c1d, M1D_MS, signal_close_time)

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
                LOGGER.exception(
                    "BACKTEST ENGINE failed | %s | signal=%d",
                    history.symbol,
                    signal_close_time,
                )
                continue

            if not analysis.get("technical_candidate"):
                continue

            side = str(analysis.get("setup") or "").upper()
            if side not in {"LONG", "SHORT"}:
                continue

            if history.symbol.upper() == "BTC_USDT":
                btc_ok = True
            else:
                btc_c4_end = bisect_right(
                    btc4_times,
                    signal_close_time - M4H_MS,
                )
                btc_c1_end = bisect_right(
                    btc1_times,
                    signal_close_time - M1H_MS,
                )
                btc_c15_end = bisect_right(
                    btc15_times,
                    signal_close_time - M15_MS,
                )

                try:
                    btc_context = build_btc_context(
                        btc_c4_engine[:btc_c4_end],
                        btc_c1_engine[:btc_c1_end],
                        btc_c15_engine[:btc_c15_end],
                    )
                    btc_ok, _ = btc_filter_ok(
                        side,
                        btc_context,
                        is_btc=False,
                    )
                except Exception:
                    LOGGER.exception(
                        "BACKTEST BTC filter failed | %s | signal=%d",
                        history.symbol,
                        signal_close_time,
                    )
                    btc_ok = False

            if not btc_ok:
                continue

            future_start = bisect_right(
                raw_c5_times,
                signal_close_time - 1,
            )
            future_end = bisect_right(
                raw_c5_times,
                period_end - 1,
            )

            if future_start >= future_end:
                continue

            future_candles = raw_c5[future_start:future_end]
            if not future_candles:
                continue

            try:
                trade = simulate_trade(
                    analysis,
                    future_candles,
                    signal_close_time_ms=signal_close_time,
                )
            except Exception:
                LOGGER.exception(
                    "BACKTEST SIMULATION failed | %s | signal=%d",
                    history.symbol,
                    signal_close_time,
                )
                continue

            if trade is not None:
                trades.append(trade)

        return trades
