from __future__ import annotations

import asyncio
import logging
import multiprocessing
import threading
import time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from ..analysis.engine import (
    _bos_events,
    _five_minute_trigger,  # compatibility export only; NOT used by the prefilter
    _fifteen_minute_entry_confirmation,
    _four_hour_regime,
    _one_hour_alignment,
    _pullback_retest,
    analyze_candles,
    build_btc_context,
    btc_filter_ok,
    convert_candles,
)
from ..automation.mexc_client import MexcClient
from ..automation.universe import MexcUniverse
from ..config import Settings
from .report import BacktestSummary, summarize
from .simulator import (
    DEFAULT_FEE_RATE,
    DEFAULT_MAX_HOLDING_MINUTES,
    DEFAULT_SLIPPAGE_BPS,
    SimulatedTrade,
    simulate_trade,
)

LOGGER = logging.getLogger(__name__)

M5_MS = 300_000
M15_MS = 900_000
H1_MS = 3_600_000
H4_MS = 14_400_000
D1_MS = 86_400_000
INTERVALS = {"4h": "Hour4", "1h": "Min60", "15m": "Min15", "5m": "Min5", "1d": "Day1"}
MAX_KLINE_POINTS = 2000
REQUEST_TIMEOUT_SECONDS = 30
MAX_SYMBOL_CONCURRENCY = 2
SYMBOL_FETCH_TIMEOUT_SECONDS = 120
SYMBOL_ANALYSIS_TIMEOUT_SECONDS = 90
HEARTBEAT_INTERVAL_SECONDS = 30
MIN_4H_WARMUP_MS = 45 * 24 * 60 * 60 * 1000
MIN_1H_WARMUP_MS = 21 * 24 * 60 * 60 * 1000
MIN_15M_WARMUP_MS = 7 * 24 * 60 * 60 * 1000
MIN_5M_WARMUP_MS = 2 * 24 * 60 * 60 * 1000
MIN_1D_WARMUP_MS = 60 * 24 * 60 * 60 * 1000
MAX_SETUP_AGE_15M = 8


class BacktestAlreadyRunning(RuntimeError):
    pass


def _isolated_backtest_symbol(
    history: "SymbolHistory",
    start: int,
    end: int,
    btc_history: "SymbolHistory",
    fee_rate: float,
    slippage_bps: float,
    default_max_hold_minutes: float,
    conn: Any,
    child_done: Any,
) -> None:
    """
    Execute one CPU-bound symbol analysis in a separate process.

    This is intentionally isolated from the asyncio event loop. A hung
    analyze/structure/simulation path can therefore be terminated by the
    parent worker instead of blocking a worker thread forever.
    """
    try:
        settings = SimpleNamespace(
            backtest_fee_rate=fee_rate,
            backtest_slippage_bps=slippage_bps,
            backtest_max_holding_minutes=default_max_hold_minutes,
        )
        runner = BacktestRunner(
            client=None,
            universe=None,
            settings=settings,
            max_concurrency=1,
        )
        # BTC context is memoization only; use a fresh child-local cache.
        symbol_trades = runner._backtest_symbol(
            history,
            start,
            end,
            btc_history,
            {},
        )
        conn.send(("ok", symbol_trades, dict(history.diagnostics)))
        child_done.set()
    except BaseException as exc:
        try:
            try:
                conn.send(("error", type(exc).__name__, str(exc)))
            finally:
                child_done.set()
        except Exception:
            pass
    finally:
        try:
            child_done.set()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@dataclass
class SymbolHistory:
    symbol: str
    candles_4h: list[list[float | int]]
    candles_1h: list[list[float | int]]
    candles_15m: list[list[float | int]]
    candles_5m: list[list[float | int]]
    candles_1d: list[list[float | int]]
    prefilter_candidates: tuple[tuple[int, Any], ...] = ()
    diagnostics: dict[str, int] = field(default_factory=dict)


def _row_time(row: Any) -> int:
    return int(row["time"] if isinstance(row, dict) else float(row[0]))


def _closed_slice(rows: list, interval_ms: int, close_time_ms: int) -> list:
    if not rows:
        return []
    cutoff = int(close_time_ms) - int(interval_ms)
    count = bisect_right(rows, cutoff, key=_row_time)
    return rows[:count]


async def _fetch_range(
    client: MexcClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[float | int]]:
    sizes = {"Min5": M5_MS, "Min15": M15_MS, "Min60": H1_MS, "Hour4": H4_MS, "Day1": D1_MS}
    if interval not in sizes:
        raise ValueError(f"Unsupported backtest interval: {interval}")
    step = sizes[interval]
    start = (int(start_ms) // step) * step
    end = (int(end_ms) // step) * step
    if end < start:
        return []

    result: dict[int, list] = {}
    cursor = start
    while cursor <= end:
        page_end = min(end, cursor + (MAX_KLINE_POINTS - 1) * step)
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
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{symbol} {interval}: request timed out") from exc
        except Exception as exc:
            raise RuntimeError(f"{symbol} {interval}: request failed: {type(exc).__name__}: {exc}") from exc

        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(f"{symbol} {interval}: invalid MEXC response type")

        valid: list[int] = []
        for row in rows:
            try:
                if not isinstance(row, (list, tuple)) or len(row) < 6:
                    continue
                ts = int(float(row[0]))
                if cursor <= ts <= page_end:
                    result[ts] = list(row)
                    valid.append(ts)
            except (TypeError, ValueError, IndexError):
                continue

        cursor = max(valid) + step if valid else page_end + step

    final = [result[key] for key in sorted(result)]
    LOGGER.debug("BACKTEST DATA | %s | %s | candles=%d", symbol, interval, len(final))
    return final


async def _fetch_timeframe(client: MexcClient, symbol: str, timeframe: str, interval: str, start_ms: int, end_ms: int):
    started = time.monotonic()
    LOGGER.info("BACKTEST TF FETCH START | symbol=%s timeframe=%s", symbol, timeframe)
    rows = await _fetch_range(client, symbol, interval, start_ms, end_ms)
    LOGGER.info("BACKTEST TF FETCH DONE | symbol=%s timeframe=%s candles=%d seconds=%.2f", symbol, timeframe, len(rows), time.monotonic() - started)
    return rows


class BacktestRunner:
    """Intraday historical runner with an authoritative full-engine decision path.

    The prefilter locates 15M BOS/retest windows and applies only a cached
    higher-timeframe eligibility gate. It does not decide score, momentum,
    volume, SL, TP, RR, futures context, or the final signal.
    The full engine is always authoritative at every candidate timestamp.
    """

    def __init__(self, client: MexcClient, universe: MexcUniverse, settings: Settings, *, max_concurrency: int = MAX_SYMBOL_CONCURRENCY) -> None:
        self.client = client
        self.universe = universe
        self.settings = settings
        self.max_concurrency = max(1, min(int(max_concurrency), MAX_SYMBOL_CONCURRENCY))
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    async def _heartbeat(self, state: dict[str, Any], started: float, stop_event: asyncio.Event, days: int) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
                return
            except asyncio.TimeoutError:
                LOGGER.info(
                    "BACKTEST HEARTBEAT | days=%d phase=%s processed=%d/%d tested=%d data_errors=%d engine_errors=%d simulation_errors=%d signals=%d worker=%s symbol=%s elapsed=%.1fs",
                    days, state.get("phase"), state.get("processed", 0), state.get("total", 0),
                    state.get("tested", 0), state.get("data_errors", 0), state.get("engine_errors", 0),
                    state.get("simulation_errors", 0), state.get("signals", 0), state.get("worker", "-"),
                    state.get("symbol", "-"), time.monotonic() - started,
                )

    async def _run_symbol_analysis_with_timeout(
        self,
        history: SymbolHistory,
        start: int,
        end: int,
        btc_history: SymbolHistory,
    ) -> tuple[list[SimulatedTrade], dict[str, int]]:
        """Run one symbol in an isolated spawn process with an independent watchdog.

        The watchdog lives in a daemon thread in the parent process, not in the
        asyncio event loop. Therefore a delayed/stalled event loop cannot prevent
        the 90-second child kill from happening. Spawn is used deliberately: the
        live FastAPI/asyncio process is never forked.
        """
        fee_rate = float(
            getattr(self.settings, "backtest_fee_rate", DEFAULT_FEE_RATE)
            if self.settings is not None else DEFAULT_FEE_RATE
        )
        slippage_bps = float(
            getattr(self.settings, "backtest_slippage_bps", DEFAULT_SLIPPAGE_BPS)
            if self.settings is not None else DEFAULT_SLIPPAGE_BPS
        )
        default_max_hold_minutes = float(
            getattr(self.settings, "backtest_max_holding_minutes", DEFAULT_MAX_HOLDING_MINUTES)
            if self.settings is not None else DEFAULT_MAX_HOLDING_MINUTES
        )

        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        child_done = ctx.Event()
        process = ctx.Process(
            target=_isolated_backtest_symbol,
            args=(
                history,
                start,
                end,
                btc_history,
                fee_rate,
                slippage_bps,
                default_max_hold_minutes,
                child_conn,
                child_done,
            ),
            name=f"backtest-analysis-{history.symbol}",
        )

        started = time.monotonic()
        watchdog_stop = threading.Event()
        watchdog_timeout = threading.Event()

        def watchdog() -> None:
            if watchdog_stop.wait(SYMBOL_ANALYSIS_TIMEOUT_SECONDS):
                return
            if child_done.is_set() or not process.is_alive():
                return
            watchdog_timeout.set()
            LOGGER.error(
                "BACKTEST WATCHDOG TIMEOUT | symbol=%s | pid=%s | timeout=%ss",
                history.symbol, process.pid, SYMBOL_ANALYSIS_TIMEOUT_SECONDS,
            )
            try:
                process.terminate()
            except Exception:
                LOGGER.exception("BACKTEST WATCHDOG TERMINATE FAILED | symbol=%s", history.symbol)
                return
            try:
                process.join(3.0)
            except Exception:
                LOGGER.exception("BACKTEST WATCHDOG JOIN FAILED | symbol=%s", history.symbol)
            if process.is_alive():
                try:
                    process.kill()
                    process.join(2.0)
                except Exception:
                    LOGGER.exception("BACKTEST WATCHDOG KILL FAILED | symbol=%s", history.symbol)
            LOGGER.error(
                "BACKTEST WATCHDOG KILLED | symbol=%s | pid=%s | exitcode=%s",
                history.symbol, process.pid, process.exitcode,
            )

        watchdog_thread: threading.Thread | None = None
        try:
            LOGGER.info("BACKTEST ANALYSIS PROCESS START | symbol=%s", history.symbol)
            # Never fork the live FastAPI/asyncio process. Spawn has clean state.
            # Start it off the event loop because process.start() itself can block.
            await asyncio.to_thread(process.start)
            child_conn.close()
            LOGGER.info(
                "BACKTEST ANALYSIS PROCESS STARTED | symbol=%s | pid=%s | method=spawn",
                history.symbol, process.pid,
            )

            watchdog_thread = threading.Thread(
                target=watchdog,
                name=f"backtest-watchdog-{history.symbol}",
                daemon=True,
            )
            watchdog_thread.start()

            payload = None
            while True:
                if parent_conn.poll(0):
                    try:
                        payload = parent_conn.recv()
                    except (EOFError, OSError):
                        payload = None
                    break
                if not process.is_alive():
                    break
                if watchdog_timeout.is_set():
                    raise TimeoutError(
                        f"{history.symbol}: analysis exceeded {SYMBOL_ANALYSIS_TIMEOUT_SECONDS}s"
                    )
                await asyncio.sleep(0.20)

            if watchdog_timeout.is_set():
                raise TimeoutError(
                    f"{history.symbol}: analysis exceeded {SYMBOL_ANALYSIS_TIMEOUT_SECONDS}s"
                )

            # The child may have exited between the last poll and this check.
            if payload is None and parent_conn.poll(0):
                try:
                    payload = parent_conn.recv()
                except (EOFError, OSError):
                    payload = None

            if payload is None:
                raise RuntimeError(
                    f"{history.symbol}: analysis subprocess exited without a result "
                    f"(exitcode={process.exitcode})"
                )
            if payload[0] == "error":
                raise RuntimeError(
                    f"{history.symbol}: isolated analysis failed: {payload[1]}: {payload[2]}"
                )
            return payload[1], payload[2]
        except asyncio.CancelledError:
            if process.is_alive():
                try:
                    process.terminate()
                except Exception:
                    pass
                await asyncio.to_thread(process.join, 3.0)
                if process.is_alive():
                    try:
                        process.kill()
                    except Exception:
                        pass
                    await asyncio.to_thread(process.join, 2.0)
            raise
        finally:
            watchdog_stop.set()
            if watchdog_thread is not None and watchdog_thread.is_alive():
                watchdog_thread.join(timeout=1.0)
            try:
                child_conn.close()
            except Exception:
                pass
            try:
                parent_conn.close()
            except Exception:
                pass
            if process.is_alive():
                try:
                    process.terminate()
                except Exception:
                    pass
                await asyncio.to_thread(process.join, 2.0)
                if process.is_alive():
                    try:
                        process.kill()
                    except Exception:
                        pass
                    await asyncio.to_thread(process.join, 2.0)

    async def _fetch_btc_history(self, start: int, end: int) -> SymbolHistory:
        starts = {
            "4h": start - MIN_4H_WARMUP_MS,
            "1h": start - MIN_1H_WARMUP_MS,
            "15m": start - MIN_15M_WARMUP_MS,
            "5m": start - MIN_5M_WARMUP_MS,
            "1d": start - MIN_1D_WARMUP_MS,
        }
        tasks = [
            asyncio.create_task(_fetch_timeframe(self.client, "BTC_USDT", "4H", INTERVALS["4h"], starts["4h"], end)),
            asyncio.create_task(_fetch_timeframe(self.client, "BTC_USDT", "1H", INTERVALS["1h"], starts["1h"], end)),
            asyncio.create_task(_fetch_timeframe(self.client, "BTC_USDT", "15M", INTERVALS["15m"], starts["15m"], end)),
            asyncio.create_task(_fetch_timeframe(self.client, "BTC_USDT", "5M", INTERVALS["5m"], starts["5m"], end)),
            asyncio.create_task(_fetch_timeframe(self.client, "BTC_USDT", "1D", INTERVALS["1d"], starts["1d"], end)),
        ]
        try:
            rows = await asyncio.wait_for(asyncio.gather(*tasks), timeout=SYMBOL_FETCH_TIMEOUT_SECONDS)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        c4, c1, c15, c5, c1d = rows
        c4 = [r for r in c4 if _row_time(r) + H4_MS <= end]
        c1 = [r for r in c1 if _row_time(r) + H1_MS <= end]
        c15 = [r for r in c15 if _row_time(r) + M15_MS <= end]
        c5 = [r for r in c5 if _row_time(r) + M5_MS <= end]
        c1d = [r for r in c1d if _row_time(r) + D1_MS <= end]
        return SymbolHistory("BTC_USDT", c4, c1, c15, c5, c1d)

    @staticmethod
    def _find_15m_setup_windows(c15: list, period_start: int, period_end: int, diagnostics: dict[str, int]) -> tuple[tuple[int, Any], ...]:
        """Find only structurally eligible 15M windows.

        This remains a prefilter: the full analysis engine is authoritative.
        Candidate metadata carries the originating BOS/retest so the runner can
        cheaply verify higher-timeframe alignment before invoking the full engine.
        """
        candidates: dict[tuple[int, str], dict[str, Any]] = {}
        open_times = [_row_time(row) for row in c15]
        for side in ("LONG", "SHORT"):
            bos_events = _bos_events(c15, side, lookback=len(c15))
            diagnostics[f"BOS_{side}"] = diagnostics.get(f"BOS_{side}", 0) + len(bos_events)
            for bos in bos_events:
                retest = _pullback_retest(c15, side, bos, MAX_SETUP_AGE_15M)
                if not retest.get("valid"):
                    continue
                diagnostics[f"RETEST_{side}"] = diagnostics.get(f"RETEST_{side}", 0) + 1
                retest_time = int(retest["time"])
                first_index = bisect_right(open_times, retest_time)
                last_close_time = min(period_end, retest_time + MAX_SETUP_AGE_15M * M15_MS)
                last_index = bisect_right(open_times, last_close_time - M15_MS)
                for index in range(first_index, min(last_index + 1, len(c15))):
                    close_time = open_times[index] + M15_MS
                    if close_time < period_start or close_time > period_end:
                        continue
                    candidates.setdefault((close_time, side), {
                        "side": side,
                        "bos_level": float(bos["level"]),
                        "bos_time": int(bos["time"]),
                        "retest_time": retest_time,
                    })
        diagnostics["SETUP_WINDOWS_15M"] = diagnostics.get("SETUP_WINDOWS_15M", 0) + len(candidates)
        return tuple((timestamp, meta) for (timestamp, _side), meta in sorted(candidates.items(), key=lambda item: (item[0][0], item[0][1])))

    async def _prepare_symbol_history(self, symbol: str, start: int, end: int) -> SymbolHistory:
        starts = {
            "4h": start - MIN_4H_WARMUP_MS,
            "1h": start - MIN_1H_WARMUP_MS,
            "15m": start - MIN_15M_WARMUP_MS,
            "5m": start - MIN_5M_WARMUP_MS,
            "1d": start - MIN_1D_WARMUP_MS,
        }
        tasks = [
            asyncio.create_task(_fetch_timeframe(self.client, symbol, "4H", INTERVALS["4h"], starts["4h"], end)),
            asyncio.create_task(_fetch_timeframe(self.client, symbol, "1H", INTERVALS["1h"], starts["1h"], end)),
            asyncio.create_task(_fetch_timeframe(self.client, symbol, "15M", INTERVALS["15m"], starts["15m"], end)),
        ]
        try:
            c4_raw, c1_raw, c15_raw = await asyncio.wait_for(asyncio.gather(*tasks), timeout=SYMBOL_FETCH_TIMEOUT_SECONDS)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.ca
