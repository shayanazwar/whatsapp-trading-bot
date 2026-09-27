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
                          
