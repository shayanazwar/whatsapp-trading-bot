from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import pickle
import tempfile
import threading
import time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from ..analysis.engine import (
    _bos_events,
    _build_15m_backtest_context,
    _five_minute_trigger,
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

M5_MS = 300000
M15_MS = 900000
H1_MS = 3600000
H4_MS = 14400000
D1_MS = 86400000

INTERVALS = {
    "4h": "Hour4",
    "1h": "Min60",
    "15m": "Min15",
    "5m": "Min5",
    "1d": "Day1",
}

MAX_KLINE_POINTS = 2000
REQUEST_TIMEOUT_SECONDS = 30
MAX_SYMBOL_CONCURRENCY = 2
MAX_BACKTEST_SYMBOLS = 200
SYMBOL_FETCH_TIMEOUT_SECONDS = 120
CHILD_BOOT_TIMEOUT_SECONDS = 45

# Emergency ceiling only. Normal symbols should finish far sooner.
SYMBOL_ANALYSIS_TIMEOUT_SECONDS = 180

HEARTBEAT_INTERVAL_SECONDS = 30

CHILD_PROGRESS_INTERVAL_CALLS = 25
CHILD_PROGRESS_INTERVAL_SECONDS = 20.0


def _send_child_status(conn: Any, stage: str, details: dict[str, Any]) -> None:
    """Send best-effort progress without allowing IPC errors to stop analysis."""
    try:
        conn.send(("status", stage, details))
    except (BrokenPipeError, EOFError, OSError):
        pass
    except Exception:
        LOGGER.debug("BACKTEST CHILD STATUS SEND FAILED", exc_info=True)

MIN_4H_WARMUP_MS = 45 * 24 * 60 * 60 * 1000
MIN_1H_WARMUP_MS = 21 * 24 * 60 * 60 * 1000
MIN_15M_WARMUP_MS = 7 * 24 * 60 * 60 * 1000
MIN_5M_WARMUP_MS = 2 * 24 * 60 * 60 * 1000
MIN_1D_WARMUP_MS = 60 * 24 * 60 * 60 * 1000

MAX_SETUP_AGE_15M = 8


class BacktestAlreadyRunning(RuntimeError):
    pass



def _isolated_backtest_symbol(
    payload_path: str,
    start: int,
    end: int,
    fee_rate: float,
    slippage_bps: float,
    default_max_hold_minutes: float,
    conn: Any,
    child_done: Any,
) -> None:
    """Run one symbol in an isolated process using a file-backed payload.

    Keeping the large candle payload out of multiprocessing ``spawn`` avoids
    serializing hundreds of thousands of Python objects through Process.start.
    """
    try:
        settings = SimpleNamespace(
            backtest_fee_rate=fee_rate,
            backtest_slippage_bps=slippage_bps,
            backtest_max_holding_minutes=default_max_hold_minutes,
        )

        with open(payload_path, "rb") as handle:
            history, btc_history = pickle.load(handle)

        LOGGER.info(
            "BACKTEST CHILD START | symbol=%s candidates=%d pid=%s",
            history.symbol,
            len(getattr(history, "prefilter_candidates", ()) or ()),
            multiprocessing.current_process().pid,
        )

        _send_child_status(
            conn,
            "CHILD_READY",
            {
                "pid": multiprocessing.current_process().pid,
                "symbol": history.symbol,
                "candidates": len(
                    getattr(history, "prefilter_candidates", ()) or ()
                ),
            },
        )

        runner = BacktestRunner(
            client=None,
            universe=None,
            settings=settings,
            max_concurrency=1,
        )

        started = time.monotonic()

        child_pid = multiprocessing.current_process().pid

        def report_progress(stage: str, details: dict[str, Any] | None = None) -> None:
            status = dict(details or {})
            status.setdefault("symbol", history.symbol)
            status["pid"] = child_pid
            _send_child_status(conn, stage, status)

        symbol_trades = runner._backtest_symbol(
            history,
            start,
            end,
            btc_history,
            {},
            progress_callback=report_progress,
        )

        LOGGER.info(
            "BACKTEST CHILD COMPLETE | symbol=%s trades=%d seconds=%.2f",
            history.symbol,
            len(symbol_trades),
            time.monotonic() - started,
        )

        conn.send(
            (
                "ok",
                symbol_trades,
                dict(history.diagnostics),
            )
        )
    except BaseException as exc:
        LOGGER.exception(
            "BACKTEST CHILD ERROR | type=%s message=%s",
            type(exc).__name__,
            exc,
        )
        try:
            conn.send(
                (
                    "error",
                    type(exc).__name__,
                    str(exc),
                )
            )
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
    return int(
        row["time"]
        if isinstance(row, dict)
        else float(row[0])
    )


def _closed_slice(
    rows: list,
    interval_ms: int,
    close_time_ms: int,
) -> list:
    if not rows:
        return []

    cutoff = int(close_time_ms) - int(interval_ms)

    count = bisect_right(
        rows,
        cutoff,
        key=_row_time,
    )

    return rows[:count]


def _safe_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        return float(value)
    except Exception:
        return default


async def _fetch_range(
    client: MexcClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[float | int]]:

    sizes = {
        "Min5": M5_MS,
        "Min15": M15_MS,
        "Min60": H1_MS,
        "Hour4": H4_MS,
        "Day1": D1_MS,
    }

    if interval not in sizes:
        raise ValueError(
            f"Unsupported backtest interval: {interval}"
        )

    step = sizes[interval]

    start = (
        int(start_ms)
        // step
        * step
    )

    end = (
        int(end_ms)
        // step
        * step
    )

    if end < start:
        return []

    result: dict[int, list] = {}

    cursor = start

    while cursor <= end:
        page_end = min(
            end,
            cursor
            + (MAX_KLINE_POINTS - 1)
            * step,
        )

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
            raise TimeoutError(
                f"{symbol} {interval}: request timed out"
            ) from exc

        except Exception as exc:
            raise RuntimeError(
                f"{symbol} {interval}: request failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if rows is None:
            rows = []

        if not isinstance(rows, list):
            raise ValueError(
                f"{symbol} {interval}: "
                "invalid MEXC response type"
            )

        valid: list[int] = []

        for row in rows:
            try:
                if not isinstance(
                    row,
                    (list, tuple),
                ):
                    continue

                if len(row) < 6:
                    continue

                ts = int(
                    float(row[0])
                )

                if (
                    cursor
                    <= ts
                    <= page_end
                ):
                    result[ts] = list(row)
                    valid.append(ts)

            except (
                TypeError,
                ValueError,
                IndexError,
            ):
                continue

        if valid:
            cursor = (
                max(valid)
                + step
            )
        else:
            cursor = (
                page_end
                + step
            )

    final = [
        result[key]
        for key in sorted(result)
    ]

    LOGGER.debug(
        "BACKTEST DATA | %s | %s | candles=%d",
        symbol,
        interval,
        len(final),
    )

    return final


async def _fetch_timeframe(
    client: MexcClient,
    symbol: str,
    timeframe: str,
    interval: str,
    start_ms: int,
    end_ms: int,
):
    started = time.monotonic()

    LOGGER.info(
        "BACKTEST TF FETCH START | "
        "symbol=%s timeframe=%s",
        symbol,
        timeframe,
    )

    rows = await _fetch_range(
        client,
        symbol,
        interval,
        start_ms,
        end_ms,
    )

    LOGGER.info(
        "BACKTEST TF FETCH DONE | "
        "symbol=%s timeframe=%s "
        "candles=%d seconds=%.2f",
        symbol,
        timeframe,
        len(rows),
        time.monotonic() - started,
    )

    return rows


class BacktestRunner:

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
            min(
                int(max_concurrency),
                MAX_SYMBOL_CONCURRENCY,
            ),
        )

        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    async def _heartbeat(
        self,
        state: dict[str, Any],
        started: float,
        stop_event: asyncio.Event,
        days: int,
    ) -> None:

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )
                return

            except asyncio.TimeoutError:
                LOGGER.info(
                    "BACKTEST HEARTBEAT | "
                    "days=%d phase=%s "
                    "processed=%d/%d "
                    "tested=%d "
                    "data_errors=%d "
                    "engine_errors=%d "
                    "simulation_errors=%d "
                    "signals=%d "
                    "worker=%s symbol=%s "
                    "child_stage=%s candidate=%s/%s engine_calls=%s "
                    "engine_seconds=%s last_engine_seconds=%s "
                    "elapsed=%.1fs",
                    days,
                    state.get("phase"),
                    state.get("processed", 0),
                    state.get("total", 0),
                    state.get("tested", 0),
                    state.get("data_errors", 0),
                    state.get("engine_errors", 0),
                    state.get(
                        "simulation_errors",
                        0,
                    ),
                    state.get("signals", 0),
                    state.get("worker", "-"),
                    state.get("symbol", "-"),
                    state.get("child_stage", "-"),
                    state.get("candidate", "-"),
                    state.get("candidate_total", "-"),
                    state.get("engine_calls", 0),
                    state.get("engine_seconds", 0),
                    state.get("last_engine_seconds", 0),
                    time.monotonic()
                    - started,
                )

    async def _run_symbol_analysis_with_timeout(
        self,
        history: SymbolHistory,
        start: int,
        end: int,
        btc_history: SymbolHistory,
        state: dict[str, Any] | None = None,
    ) -> tuple[list[SimulatedTrade], dict[str, int]]:
        """Run one symbol in an isolated subprocess with a hard watchdog."""
        fee_rate = float(
            getattr(
                self.settings,
                "backtest_fee_rate",
                DEFAULT_FEE_RATE,
            )
            if self.settings is not None
            else DEFAULT_FEE_RATE
        )
        slippage_bps = float(
            getattr(
                self.settings,
                "backtest_slippage_bps",
                DEFAULT_SLIPPAGE_BPS,
            )
            if self.settings is not None
            else DEFAULT_SLIPPAGE_BPS
        )
        default_max_hold_minutes = float(
            getattr(
                self.settings,
                "backtest_max_holding_minutes",
                DEFAULT_MAX_HOLDING_MINUTES,
            )
            if self.settings is not None
            else DEFAULT_MAX_HOLDING_MINUTES
        )

        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        child_done = ctx.Event()

        payload_path: str | None = None
        process: multiprocessing.Process | None = None
        watchdog_stop = threading.Event()
        watchdog_timeout = threading.Event()
        child_booted = threading.Event()
        watchdog_reason = [""]
        watchdog_thread: threading.Thread | None = None
        # Each invocation owns its progress snapshot. The shared run state is
        # only an observability index; it must never be used for timeout data.
        progress_lock = threading.Lock()
        worker_progress: dict[str, Any] = {
            "stage": "PROCESS_START",
            "details": {"symbol": history.symbol, "pid": None},
        }
        progress_key: tuple[str, int | None] = (history.symbol, None)

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f"pta-backtest-{history.symbol}-",
                suffix=".pkl",
                delete=False,
            ) as handle:
                payload_path = handle.name
                pickle.dump(
                    (history, btc_history),
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
                handle.flush()

            process = ctx.Process(
                target=_isolated_backtest_symbol,
                args=(
                    payload_path,
                    start,
                    end,
                    fee_rate,
                    slippage_bps,
                    default_max_hold_minutes,
                    child_conn,
                    child_done,
                ),
                name=f"backtest-analysis-{history.symbol}",
            )
            process.daemon = True

            def watchdog() -> None:
                if not child_booted.wait(CHILD_BOOT_TIMEOUT_SECONDS):
                    if watchdog_stop.is_set() or child_done.is_set():
                        return
                    watchdog_reason[0] = "child boot timeout"
                elif watchdog_stop.wait(SYMBOL_ANALYSIS_TIMEOUT_SECONDS):
                    return
                if child_done.is_set() or process is None:
                    return
                if not process.is_alive():
                    return

                watchdog_timeout.set()
                with progress_lock:
                    latest_progress = dict(worker_progress)
                LOGGER.error(
                    "BACKTEST WATCHDOG TIMEOUT | symbol=%s | pid=%s | reason=%s | last_stage=%s | last_progress=%s | boot_timeout=%ss | analysis_timeout=%ss",
                    history.symbol,
                    process.pid,
                    watchdog_reason[0] or "analysis timeout",
                    latest_progress["stage"],
                    latest_progress["details"],
                    CHILD_BOOT_TIMEOUT_SECONDS,
                    SYMBOL_ANALYSIS_TIMEOUT_SECONDS,
                )
                try:
                    process.terminate()
                except Exception:
                    LOGGER.exception(
                        "BACKTEST WATCHDOG TERMINATE FAILED | symbol=%s",
                        history.symbol,
                    )
                    return

                try:
                    process.join(3.0)
                except Exception:
                    LOGGER.exception(
                        "BACKTEST WATCHDOG JOIN FAILED | symbol=%s",
                        history.symbol,
                    )

                if process.is_alive():
                    try:
                        process.kill()
                        process.join(2.0)
                    except Exception:
                        LOGGER.exception(
                            "BACKTEST WATCHDOG KILL FAILED | symbol=%s",
                            history.symbol,
                        )

                LOGGER.error(
                    "BACKTEST WATCHDOG KILLED | symbol=%s | pid=%s | exitcode=%s",
                    history.symbol,
                    process.pid,
                    process.exitcode,
                )

            payload_kb = (
                os.path.getsize(payload_path) / 1024.0
                if payload_path
                else 0.0
            )
            LOGGER.info(
                "BACKTEST ANALYSIS PROCESS START | symbol=%s | payload_kb=%.1f",
                history.symbol,
                payload_kb,
            )

            await asyncio.to_thread(process.start)

            try:
                child_conn.close()
            except Exception:
                pass

            LOGGER.info(
                "BACKTEST ANALYSIS PROCESS STARTED | symbol=%s | pid=%s | method=spawn | payload=FILE",
                history.symbol,
                process.pid,
            )

            watchdog_thread = threading.Thread(
                target=watchdog,
                name=f"backtest-watchdog-{history.symbol}",
                daemon=True,
            )
            watchdog_thread.start()

            payload = None
            analysis_deadline = time.monotonic() + SYMBOL_ANALYSIS_TIMEOUT_SECONDS
            while True:
                if parent_conn.poll(0):
                    try:
                        message = parent_conn.recv()
                    except (EOFError, OSError):
                        message = None

                    if message and message[0] == "status":
                        _, child_stage, details = message
                        if not isinstance(details, dict):
                            details = {"value": details}
                        message_symbol = str(details.get("symbol", history.symbol))
                        message_pid = details.get("pid", process.pid)
                        # This pipe belongs to this child, but validate identity
                        # before accepting state so malformed/stale IPC cannot
                        # replace another worker's diagnostic.
                        if message_symbol != history.symbol or message_pid != process.pid:
                            LOGGER.warning(
                                "BACKTEST CHILD STATUS IDENTITY MISMATCH | expected=%s/%s got=%s/%s",
                                history.symbol, process.pid, message_symbol, message_pid,
                            )
                            continue
                        child_booted.set()
                        with progress_lock:
                            worker_progress["stage"] = str(child_stage)
                            worker_progress["details"] = dict(details)
                            progress_key = (history.symbol, process.pid)
                        if state is not None:
                            workers = state.setdefault("child_workers", {})
                            workers[progress_key] = {
                                "stage": str(child_stage),
                                "details": dict(details),
                            }
                            # Keep legacy heartbeat fields as most recently
                            # received, but never consult them for watchdogs.
                            state["child_stage"] = str(child_stage)
                            state["child_progress"] = dict(details)
                            state["child_pid"] = process.pid
                            state["candidate"] = details.get("candidate", "-")
                            state["candidate_total"] = details.get("candidates", "-")
                            state["engine_calls"] = details.get("engine_calls", 0)
                            state["engine_seconds"] = details.get("engine_seconds", 0)
                            state["last_engine_seconds"] = details.get("last_engine_seconds", 0)
                        LOGGER.info(
                            "BACKTEST CHILD PROGRESS | symbol=%s pid=%s stage=%s details=%s",
                            history.symbol,
                            process.pid,
                            child_stage,
                            details,
                        )
                        continue

                    payload = message
                    break

                if time.monotonic() >= analysis_deadline and not watchdog_timeout.is_set():
                    watchdog_timeout.set()
                    watchdog_reason[0] = watchdog_reason[0] or "analysis timeout (parent deadline)"
                    with progress_lock:
                        latest_progress = dict(worker_progress)
                    LOGGER.error(
                        "BACKTEST WATCHDOG TIMEOUT | symbol=%s | pid=%s | reason=%s | last_stage=%s | last_progress=%s | boot_timeout=%ss | analysis_timeout=%ss",
                        history.symbol, process.pid, watchdog_reason[0], latest_progress["stage"], latest_progress["details"], CHILD_BOOT_TIMEOUT_SECONDS, SYMBOL_ANALYSIS_TIMEOUT_SECONDS,
                    )
                    if process.is_alive():
                        try:
                            process.terminate()
                            await asyncio.to_thread(process.join, 3.0)
                        except Exception:
                            LOGGER.exception("BACKTEST WATCHDOG TERMINATE FAILED | symbol=%s", history.symbol)
                        if process.is_alive():
                            try:
                                process.kill()
                                await asyncio.to_thread(process.join, 2.0)
                            except Exception:
                                LOGGER.exception("BACKTEST WATCHDOG KILL FAILED | symbol=%s", history.symbol)
                if watchdog_timeout.is_set():
                    with progress_lock:
                        latest_progress = dict(worker_progress)
                    raise TimeoutError(
                        f"{history.symbol}: {watchdog_reason[0] or 'analysis timeout'}; "
                        f"pid={process.pid}; last_stage={latest_progress['stage']}; "
                        f"last_progress={latest_progress['details']!r}; "
                        f"boot_limit={CHILD_BOOT_TIMEOUT_SECONDS}s; "
                        f"analysis_limit={SYMBOL_ANALYSIS_TIMEOUT_SECONDS}s"
                    )

                if not process.is_alive():
                    break

                await asyncio.sleep(0.10)

            if (
                payload is None
                and parent_conn.poll(0)
            ):
                try:
                    payload = parent_conn.recv()
                except (EOFError, OSError):
                    payload = None

            if watchdog_timeout.is_set():
                with progress_lock:
                    latest_progress = dict(worker_progress)
                raise TimeoutError(
                    f"{history.symbol}: {watchdog_reason[0] or 'analysis timeout'}; "
                    f"pid={process.pid}; last_stage={latest_progress['stage']}; "
                    f"last_progress={latest_progress['details']!r}; "
                    f"boot_limit={CHILD_BOOT_TIMEOUT_SECONDS}s; "
                    f"analysis_limit={SYMBOL_ANALYSIS_TIMEOUT_SECONDS}s"
                )

            if payload is None:
                raise RuntimeError(
                    f"{history.symbol}: analysis subprocess exited "
                    f"without a result (exitcode={process.exitcode})"
                )

            if payload[0] == "error":
                raise RuntimeError(
                    f"{history.symbol}: isolated analysis failed: "
                    f"{payload[1]}: {payload[2]}"
                )

            if payload[0] != "ok":
                raise RuntimeError(
                    f"{history.symbol}: invalid analysis subprocess payload"
                )

            return payload[1], payload[2]

        except asyncio.CancelledError:
            if process is not None and process.is_alive():
                try:
                    process.terminate()
                except Exception:
                    pass
                try:
                    await asyncio.to_thread(process.join, 3.0)
                except Exception:
                    pass
                if process.is_alive():
                    try:
                        process.kill()
                    except Exception:
                        pass
                    try:
                        await asyncio.to_thread(process.join, 2.0)
                    except Exception:
                        pass
            raise

        finally:
            watchdog_stop.set()

            if watchdog_thread is not None and watchdog_thread.is_alive():
                try:
                    watchdog_thread.join(timeout=1.0)
                except Exception:
                    pass

            try:
                child_conn.close()
            except Exception:
                pass
            try:
                parent_conn.close()
            except Exception:
                pass

            if process is not None:
                if process.is_alive():
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    try:
                        await asyncio.to_thread(process.join, 2.0)
                    except Exception:
                        pass
                    if process.is_alive():
                        try:
                            process.kill()
                        except Exception:
                            pass
                        try:
                            await asyncio.to_thread(process.join, 2.0)
                        except Exception:
                            pass
                try:
                    process.close()
                except Exception:
                    pass

            if payload_path:
                try:
                    os.unlink(payload_path)
                except FileNotFoundError:
                    pass
                except Exception:
                    LOGGER.warning(
                        "BACKTEST TEMP PAYLOAD CLEANUP FAILED | symbol=%s | path=%s",
                        history.symbol,
                        payload_path,
                    )

    async def _fetch_btc_history(
        self,
        start: int,
        end: int,
    ) -> SymbolHistory:

        starts = {
            "4h": (
                start
                - MIN_4H_WARMUP_MS
            ),
            "1h": (
                start
                - MIN_1H_WARMUP_MS
            ),
            "15m": (
                start
                - MIN_15M_WARMUP_MS
            ),
            "5m": (
                start
                - MIN_5M_WARMUP_MS
            ),
            "1d": (
                start
                - MIN_1D_WARMUP_MS
            ),
        }

        tasks = [
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    "BTC_USDT",
                    "4H",
                    INTERVALS["4h"],
                    starts["4h"],
                    end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    "BTC_USDT",
                    "1H",
                    INTERVALS["1h"],
                    starts["1h"],
                    end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    "BTC_USDT",
                    "15M",
                    INTERVALS["15m"],
                    starts["15m"],
                    end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    "BTC_USDT",
                    "5M",
                    INTERVALS["5m"],
                    starts["5m"],
                    end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    "BTC_USDT",
                    "1D",
                    INTERVALS["1d"],
                    starts["1d"],
                    end,
                )
            ),
        ]

        try:
            rows = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=(
                    SYMBOL_FETCH_TIMEOUT_SECONDS
                ),
            )

        except BaseException:

            for task in tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

            raise

        (
            c4,
            c1,
            c15,
            c5,
            c1d,
        ) = rows

        c4 = [
            r
            for r in c4
            if (
                _row_time(r)
                + H4_MS
                <= end
            )
        ]

        c1 = [
            r
            for r in c1
            if (
                _row_time(r)
                + H1_MS
                <= end
            )
        ]

        c15 = [
            r
            for r in c15
            if (
                _row_time(r)
                + M15_MS
                <= end
            )
        ]

        c5 = [
            r
            for r in c5
            if (
                _row_time(r)
                + M5_MS
                <= end
            )
        ]

        c1d = [
            r
            for r in c1d
            if (
                _row_time(r)
                + D1_MS
                <= end
            )
        ]

        return SymbolHistory(
            "BTC_USDT",
            c4,
            c1,
            c15,
            c5,
            c1d,
        )

    @staticmethod
    @staticmethod
    def _find_15m_setup_windows(
        c15: list,
        period_start: int,
        period_end: int,
        diagnostics: dict[str, int],
    ) -> tuple[
        tuple[int, Any],
        ...,
    ]:
        """Build candidates equivalent to analyze_candles() at each point in time.

        The authoritative engine uses:
          1. _bos_events(..., lookback=70)
          2. newest BOS no older than MAX_SETUP_AGE_15M + 2 candles
          3. a valid post-BOS retest no older than MAX_SETUP_AGE_15M

        The previous Runner incorrectly allowed any BOS inside the 70-candle
        window as long as its retest was recent. That produced ~1,100 candidates
        per symbol, while the engine rejected nearly all of them. This function
        mirrors the engine's actual latest-BOS selection rules.
        """
        candidates: dict[
            tuple[int, str],
            dict[str, Any],
        ] = {}

        open_times = [
            _row_time(row)
            for row in c15
        ]

        setup_by_side: dict[str, list[dict[str, Any]]] = {}

        # Keep the one full-history BOS scan for performance. BOS events are
        # point-in-time causal: ATR at event i uses candles through i, and the
        # swing used by BOS is constrained by idx + 2 <= i.
        for side in ("LONG", "SHORT"):
            bos_events = _bos_events(
                c15,
                side,
                lookback=len(c15),
            )

            diagnostics[f"BOS_{side}"] = (
                diagnostics.get(f"BOS_{side}", 0)
                + len(bos_events)
            )

            setups: list[dict[str, Any]] = []

            for bos in bos_events:
                retest = _pullback_retest(
                    c15,
                    side,
                    bos,
                    MAX_SETUP_AGE_15M,
                )
                if not retest.get("valid"):
                    continue

                diagnostics[f"RETEST_{side}"] = (
                    diagnostics.get(f"RETEST_{side}", 0)
                    + 1
                )

                setups.append(
                    {
                        "bos_index": int(bos["index"]),
                        "bos_time": int(bos["time"]),
                        "bos_level": float(bos["level"]),
                        "bos_strength": _safe_float(
                            bos.get("strength"),
                            0.0,
                        ),
                        "retest_index": int(retest["index"]),
                        "retest_time": int(retest["time"]),
                        "retest_quality": _safe_float(
                            retest.get("quality"),
                            0.0,
                        ),
                        "retest_rejection": bool(
                            retest.get("rejection")
                        ),
                    }
                )

            setups.sort(
                key=lambda item: (
                    int(item["bos_index"]),
                    int(item["retest_index"]),
                )
            )
            setup_by_side[side] = setups

        # This exactly matches _select_latest_bos_with_retest():
        # latest BOS age <= MAX_SETUP_AGE_15M + 2, then retest age <= MAX_SETUP_AGE_15M.
        max_bos_age = MAX_SETUP_AGE_15M + 2

        for index, open_time in enumerate(open_times):
            close_time = int(open_time) + M15_MS

            if (
                close_time < period_start
                or close_time > period_end
            ):
                continue

            for side in ("LONG", "SHORT"):
                active_setup: dict[str, Any] | None = None

                for setup in reversed(setup_by_side[side]):
                    bos_index = int(setup["bos_index"])
                    retest_index = int(setup["retest_index"])
                    bos_time = int(setup["bos_time"])
                    retest_time = int(setup["retest_time"])

                    if bos_index > index:
                        continue

                    # Use candle timestamps for the final age test. This is
                    # equivalent to the engine's candle-index logic on regular
                    # MEXC candles and is robust to synthetic/test fixtures whose
                    # mocked indexes may not match their timestamps.
                    bos_close_time = bos_time + M15_MS
                    bos_age_ms = close_time - bos_close_time

                    if bos_age_ms < 0:
                        continue

                    if bos_age_ms > max_bos_age * M15_MS:
                        break

                    if retest_index > index:
                        continue

                    retest_close_time = retest_time + M15_MS
                    retest_age_ms = close_time - retest_close_time

                    if retest_age_ms < 0:
                        continue

                    if retest_age_ms > MAX_SETUP_AGE_15M * M15_MS:
                        continue

                    active_setup = setup
                    break

                if active_setup is None:
                    continue

                candidates[
                    (
                        close_time,
                        side,
                    )
                ] = {
                    "side": side,
                    "bos_level": float(
                        active_setup["bos_level"]
                    ),
                    "bos_time": int(
                        active_setup["bos_time"]
                    ),
                    "bos_strength": float(
                        active_setup["bos_strength"]
                    ),
                    "retest_time": int(
                        active_setup["retest_time"]
                    ),
                    "retest_index": int(
                        active_setup["retest_index"]
                    ),
                    "retest_quality": float(
                        active_setup["retest_quality"]
                    ),
                    "retest_rejection": bool(
                        active_setup["retest_rejection"]
                    ),
                }

        diagnostics["SETUP_WINDOWS_15M"] = (
            diagnostics.get(
                "SETUP_WINDOWS_15M",
                0,
            )
            + len(candidates)
        )

        return tuple(
            (
                timestamp,
                meta,
            )
            for (
                timestamp,
                _side,
            ), meta in sorted(
                candidates.items(),
                key=lambda item: (
                    item[0][0],
                    item[0][1],
                ),
            )
        )

    async def _prepare_symbol_history(
        self,
        symbol: str,
        start: int,
        end: int,
    ) -> SymbolHistory:

        starts = {
            "4h": (
                start
                - MIN_4H_WARMUP_MS
            ),
            "1h": (
                start
                - MIN_1H_WARMUP_MS
            ),
            "15m": (
                start
                - MIN_15M_WARMUP_MS
            ),
            "5m": (
                start
                - MIN_5M_WARMUP_MS
            ),
            "1d": (
                start
                - MIN_1D_WARMUP_MS
            ),
        }

        tasks = [
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "4H",
                    INTERVALS["4h"],
                    starts["4h"],
                    end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "1H",
                    INTERVALS["1h"],
                    starts["1h"],
                    end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "15M",
                    INTERVALS["15m"],
                    starts["15m"],
                    end,
                )
            ),
        ]

        try:
            (
                c4_raw,
                c1_raw,
                c15_raw,
            ) = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=(
                    SYMBOL_FETCH_TIMEOUT_SECONDS
                ),
            )

        except BaseException:

            for task in tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

            raise

        c4 = [
            c
            for c in convert_candles(
                c4_raw
            )
            if (
                int(c["time"])
                + H4_MS
                <= end
            )
        ]

        c1 = [
            c
            for c in convert_candles(
                c1_raw
            )
            if (
                int(c["time"])
                + H1_MS
                <= end
            )
        ]

        c15 = [
            c
            for c in convert_candles(
                c15_raw
            )
            if (
                int(c["time"])
                + M15_MS
                <= end
            )
        ]

        if len(c4) < 205:
            raise ValueError(
                f"{symbol}: "
                "insufficient 4H candles "
                f"({len(c4)} < 205)"
            )

        if len(c1) < 205:
            raise ValueError(
                f"{symbol}: "
                "insufficient 1H candles "
                f"({len(c1)} < 205)"
            )

        if len(c15) < 80:
            raise ValueError(
                f"{symbol}: "
                "insufficient 15M candles "
                f"({len(c15)} < 80)"
            )

        diagnostics: dict[
            str,
            int,
        ] = defaultdict(int)

        candidate_times = (
            self._find_15m_setup_windows(
                c15,
                start,
                end,
                diagnostics,
            )
        )

        if not candidate_times:

            diagnostics[
                "FIVE_MIN_FETCH_SKIPPED"
            ] += 1

            diagnostics[
                "ONE_D_FETCH_SKIPPED"
            ] += 1

            return SymbolHistory(
                symbol,
                c4_raw,
                c1_raw,
                c15_raw,
                [],
                [],
                candidate_times,
                dict(diagnostics),
            )

        t5 = asyncio.create_task(
            _fetch_timeframe(
                self.client,
                symbol,
                "5M",
                INTERVALS["5m"],
                starts["5m"],
                end,
            )
        )

        t1d = asyncio.create_task(
            _fetch_timeframe(
                self.client,
                symbol,
                "1D",
                INTERVALS["1d"],
                starts["1d"],
                end,
            )
        )

        try:
            (
                c5_raw,
                c1d_raw,
            ) = await asyncio.wait_for(
                asyncio.gather(
                    t5,
                    t1d,
                ),
                timeout=(
                    SYMBOL_FETCH_TIMEOUT_SECONDS
                ),
            )

        except BaseException:

            for task in (
                t5,
                t1d,
            ):
                if not task.done():
                    task.cancel()

            await asyncio.gather(
                t5,
                t1d,
                return_exceptions=True,
            )

            raise

        diagnostics[
            "FIVE_MIN_FETCH"
        ] += 1

        diagnostics[
            "ONE_D_FETCH"
        ] += 1

        return SymbolHistory(
            symbol,
            c4_raw,
            c1_raw,
            c15_raw,
            c5_raw,
            c1d_raw,
            candidate_times,
            dict(diagnostics),
        )

    def _backtest_symbol(
        self,
        history: SymbolHistory,
        start: int,
        end: int,
        btc_history: SymbolHistory,
        btc_context_cache: dict | None = None,
        progress_callback: Any | None = None,
    ) -> list[SimulatedTrade]:
        """Backtest one symbol using the authoritative engine as the sole signal gate.

        The Runner only supplies candidate timestamps from point-in-time BOS/retest
        detection. It does not independently reject a setup on HTF, 15M entry,
        5M trigger, score, momentum, volume, geometry, RR, or volatility. Those
        decisions belong to analyze_candles().
        """
        symbol_started = time.monotonic()

        c4 = convert_candles(history.candles_4h)
        c1 = convert_candles(history.candles_1h)
        c15 = convert_candles(history.candles_15m)
        c5 = convert_candles(history.candles_5m)
        c1d = convert_candles(history.candles_1d)

        btc4 = convert_candles(btc_history.candles_4h)
        btc1 = convert_candles(btc_history.candles_1h)
        btc15 = convert_candles(btc_history.candles_15m)

        candidates = tuple(
            getattr(
                history,
                "prefilter_candidates",
                (),
            )
            or ()
        )

        if not candidates:
            local_diag = defaultdict(int)
            candidates = self._find_15m_setup_windows(
                c15,
                start,
                end,
                local_diag,
            )
            history.diagnostics = {
                **getattr(
                    history,
                    "diagnostics",
                    {},
                ),
                **dict(local_diag),
            }

        diagnostics: defaultdict[str, int] = defaultdict(
            int,
            {
                str(key): int(value)
                for key, value in getattr(
                    history,
                    "diagnostics",
                    {},
                ).items()
            },
        )
        history.diagnostics = diagnostics
        diagnostics["CANDIDATES_INITIAL"] = len(candidates)

        LOGGER.info(
            "BACKTEST SYMBOL ANALYSIS BEGIN | symbol=%s candidates=%d",
            history.symbol,
            len(candidates),
        )

        if progress_callback is not None:
            progress_callback(
                "ANALYSIS_START",
                {
                    "symbol": history.symbol,
                    "candidate": 0,
                    "candidates": len(candidates),
                    "engine_calls": 0,
                    "engine_seconds": 0.0,
                    "pid": multiprocessing.current_process().pid,
                },
            )

        btc_times = (
            [_row_time(r) for r in btc_history.candles_4h],
            [_row_time(r) for r in btc_history.candles_1h],
            [_row_time(r) for r in btc_history.candles_15m],
        )

        if btc_context_cache is None:
            btc_context_cache = {}

        c5_times = [
            _row_time(r)
            for r in history.candles_5m
        ]

        trades: list[SimulatedTrade] = []
        previous_exit_time: int | None = None
        seen_structures: set[tuple[Any, Any, Any]] = set()

        engine_calls = 0
        engine_seconds = 0.0
        btc_seconds = 0.0
        simulation_seconds = 0.0

        evaluated_times: set[int] = set()
        last_progress = time.monotonic()

        # Build the complete 15M structure once. The engine slices this context
        # to each candidate prefix, preserving point-in-time/casual semantics
        # while avoiding a full ATR/swing/BOS rebuild for every candidate.
        backtest_15m_context = _build_15m_backtest_context(
            c15,
            progress_callback=progress_callback,
        )
        LOGGER.info(
            "BACKTEST 15M CONTEXT PRECOMPUTED | symbol=%s candles=%d bos_long=%d bos_short=%d",
            history.symbol,
            len(c15),
            len(backtest_15m_context.get("bos_long", [])),
            len(backtest_15m_context.get("bos_short", [])),
        )

        for candidate_index, (signal_close_time, _setup_hint) in enumerate(
            candidates,
            start=1,
        ):
            signal_close_time = int(signal_close_time)

            if signal_close_time < start or signal_close_time > end:
                diagnostics["CANDIDATE_OUTSIDE_PERIOD"] += 1
                continue

            # Long/short candidates can point to the same timestamp. The
            # authoritative engine chooses the direction from current HTF/15M
            # evidence, so evaluating that timestamp once is equivalent and avoids
            # duplicate full-engine work.
            if signal_close_time in evaluated_times:
                diagnostics["DUPLICATE_TIMESTAMP_SKIPPED"] += 1
                continue
            evaluated_times.add(signal_close_time)

            if (
                previous_exit_time is not None
                and signal_close_time <= previous_exit_time
            ):
                diagnostics["OVERLAPPING_SIGNAL_SKIPPED"] += 1
                continue

            c4s = _closed_slice(
                c4,
                H4_MS,
                signal_close_time,
            )
            c1s = _closed_slice(
                c1,
                H1_MS,
                signal_close_time,
            )
            c15s = _closed_slice(
                c15,
                M15_MS,
                signal_close_time,
            )
            c5s = _closed_slice(
                c5,
                M5_MS,
                signal_close_time,
            )
            c1ds = _closed_slice(
                c1d,
                D1_MS,
                signal_close_time,
            )

            if (
                len(c4s) < 205
                or len(c1s) < 205
                or len(c15s) < 80
                or len(c5s) < 30
            ):
                diagnostics["ENGINE_WARMUP_REJECT"] += 1
                continue

            engine_started = time.monotonic()

            def report_engine_progress(stage: str, details: dict[str, Any] | None = None) -> None:
                if progress_callback is None:
                    return
                status = dict(details or {})
                status.update(
                    symbol=history.symbol,
                    candidate=candidate_index,
                    candidates=len(candidates),
                    engine_calls=engine_calls,
                    elapsed_seconds=round(time.monotonic() - symbol_started, 2),
                )
                progress_callback(stage, status)

            # Surface long-running engine stages while the call is still active.
            report_engine_progress(
                "ENGINE_CALL_START",
                {"engine_seconds": round(engine_seconds, 2)},
            )

            try:
                analysis = analyze_candles(
                    history.symbol,
                    c4s,
                    c1s,
                    c15s,
                    c5s,
                    c1ds,
                    now_ms=signal_close_time,
                    cache={"_BACKTEST_PROGRESS_CALLBACK": report_engine_progress, "_BACKTEST_15M": backtest_15m_context},
                )
            except Exception:
                diagnostics["ENGINE_ERRORS"] += 1
                diagnostics["ENGINE_EXCEPTION"] += 1
                LOGGER.exception(
                    "BACKTEST ENGINE FAILED | symbol=%s | candidate=%d/%d | signal=%d",
                    history.symbol,
                    candidate_index,
                    len(candidates),
                    signal_close_time,
                )
                continue

            elapsed_engine = time.monotonic() - engine_started
            engine_seconds += elapsed_engine
            engine_calls += 1

            diagnostics["ENGINE_CALLS"] = engine_calls
            diagnostics["ENGINE_TIME_MS"] += int(elapsed_engine * 1000)

            now = time.monotonic()
            if (
                engine_calls % CHILD_PROGRESS_INTERVAL_CALLS == 0
                or now - last_progress >= CHILD_PROGRESS_INTERVAL_SECONDS
            ):
                last_progress = now
                LOGGER.info(
                    "BACKTEST ENGINE PROGRESS | symbol=%s candidate=%d/%d "
                    "full_engine_calls=%d technical_accept=%d "
                    "elapsed=%.1fs engine_seconds=%.1fs last_engine=%.3fs",
                    history.symbol,
                    candidate_index,
                    len(candidates),
                    engine_calls,
                    diagnostics["TECHNICAL_ACCEPT"],
                    time.monotonic() - symbol_started,
                    engine_seconds,
                    elapsed_engine,
                )
                if progress_callback is not None:
                    progress_callback(
                        "ENGINE_PROGRESS",
                        {
                            "symbol": history.symbol,
                            "candidate": candidate_index,
                            "candidates": len(candidates),
                            "engine_calls": engine_calls,
                            "technical_accept": diagnostics["TECHNICAL_ACCEPT"],
                            "elapsed_seconds": round(time.monotonic() - symbol_started, 2),
                            "engine_seconds": round(engine_seconds, 2),
                            "last_engine_seconds": round(elapsed_engine, 4),
                        },
                    )

            if not analysis.get("technical_candidate"):
                diagnostics["TECHNICAL_REJECT"] += 1

                failures = analysis.get(
                    "technical_gate_failures",
                    [],
                ) or []

                for reason in failures:
                    reason_text = str(reason)
                    diagnostics[f"ENGINE_REJECT_{reason_text}"] += 1

                continue

            diagnostics["TECHNICAL_ACCEPT"] += 1

            side = str(
                analysis.get("setup") or ""
            ).upper()

            if side not in {"LONG", "SHORT"}:
                diagnostics["INVALID_ENGINE_SIDE"] += 1
                continue

            diagnostics[f"FULL_ENGINE_ACCEPT_{side}"] += 1

            structure_key = (
                side,
                analysis.get("setup_bos_time"),
                analysis.get("setup_retest_time"),
            )

            if structure_key in seen_structures:
                diagnostics["DUPLICATE_STRUCTURE_SKIPPED"] += 1
                continue

            seen_structures.add(structure_key)

            symbol_upper = history.symbol.upper()

            if symbol_upper == "BTC_USDT":
                btc_ok = True
            else:
                btc_c4_end = bisect_right(
                    btc_times[0],
                    signal_close_time - H4_MS,
                )
                btc_c1_end = bisect_right(
                    btc_times[1],
                    signal_close_time - H1_MS,
                )
                btc_c15_end = bisect_right(
                    btc_times[2],
                    signal_close_time - M15_MS,
                )

                btc_key = (
                    btc_c4_end,
                    btc_c1_end,
                    btc_c15_end,
                )

                btc_started = time.monotonic()

                try:
                    cached = btc_context_cache.get(btc_key)

                    if cached is None:
                        context = build_btc_context(
                            btc4[:btc_c4_end],
                            btc1[:btc_c1_end],
                            btc15[:btc_c15_end],
                        )

                        btc_ok, btc_reason = btc_filter_ok(
                            side,
                            context,
                            is_btc=False,
                        )

                        btc_context_cache[btc_key] = (
                            context,
                            btc_reason,
                        )
                    else:
                        context, _btc_reason = cached
                        btc_ok, _ = btc_filter_ok(
                            side,
                            context,
                            is_btc=False,
                        )

                except Exception:
                    diagnostics["BTC_CONTEXT_ERRORS"] += 1
                    LOGGER.exception(
                        "BACKTEST BTC FILTER FAILED | symbol=%s | signal=%d",
                        history.symbol,
                        signal_close_time,
                    )
                    continue

                btc_seconds += time.monotonic() - btc_started

            if not btc_ok:
                diagnostics["BTC_REJECT"] += 1
                diagnostics[f"BTC_REJECT_{side}"] += 1
                continue

            diagnostics["BTC_ACCEPT"] += 1
            diagnostics[f"BTC_ACCEPT_{side}"] += 1

            future_start = bisect_right(
                c5_times,
                signal_close_time - 1,
            )
            future_end = bisect_right(
                c5_times,
                end - 1,
            )

            if future_start >= future_end:
                diagnostics["NO_FUTURE_CANDLES"] += 1
                continue

            future_candles = history.candles_5m[
                future_start:future_end
            ]

            if not future_candles:
                diagnostics["NO_FUTURE_CANDLES"] += 1
                continue

            simulation_started = time.monotonic()

            try:
                analysis_hold = analysis.get(
                    "intraday_max_hold_minutes"
                )

                if analysis_hold:
                    max_hold = float(
                        analysis_hold
                    )
                else:
                    max_hold = float(
                        getattr(
                            self.settings,
                            "backtest_max_holding_minutes",
                            DEFAULT_MAX_HOLDING_MINUTES,
                        )
                        if self.settings is not None
                        else DEFAULT_MAX_HOLDING_MINUTES
                    )

                fee_rate = float(
                    getattr(
                        self.settings,
                        "backtest_fee_rate",
                        DEFAULT_FEE_RATE,
                    )
                    if self.settings is not None
                    else DEFAULT_FEE_RATE
                )

                slippage_bps = float(
                    getattr(
                        self.settings,
                        "backtest_slippage_bps",
                        DEFAULT_SLIPPAGE_BPS,
                    )
                    if self.settings is not None
                    else DEFAULT_SLIPPAGE_BPS
                )

                trade = simulate_trade(
                    analysis,
                    future_candles,
                    signal_close_time_ms=signal_close_time,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    max_holding_minutes=max_hold,
                )

            except Exception:
                diagnostics["SIMULATION_ERRORS"] += 1
                LOGGER.exception(
                    "BACKTEST SIMULATION FAILED | symbol=%s | signal=%d",
                    history.symbol,
                    signal_close_time,
                )
                continue

            simulation_seconds += time.monotonic() - simulation_started

            if trade is None:
                diagnostics["SIMULATION_NO_TRADE"] += 1
                continue

            trades.append(trade)
            diagnostics["SIMULATION_ACCEPT"] += 1
            diagnostics[f"OUTCOME_{trade.outcome}"] += 1

            if trade.outcome == "TP2":
                diagnostics["TP2_BEFORE_SL"] += 1

            if trade.outcome == "SL":
                diagnostics["SL_OUTCOME"] += 1

            if trade.expired:
                diagnostics["EXPIRY"] += 1

            previous_exit_time = trade.exit_time_ms

        diagnostics["ENGINE_CALLS"] = engine_calls
        diagnostics["ENGINE_TIME_MS"] = int(engine_seconds * 1000)
        diagnostics["BTC_TIME_MS"] = int(btc_seconds * 1000)
        diagnostics["SIMULATION_TIME_MS"] = int(simulation_seconds * 1000)
        diagnostics["ANALYSIS_TOTAL_TIME_MS"] = int(
            (time.monotonic() - symbol_started) * 1000
        )

        reject_counts = sorted(
            (
                (key, value)
                for key, value in diagnostics.items()
                if key.startswith("ENGINE_REJECT_")
                and int(value) > 0
            ),
            key=lambda item: (-int(item[1]), item[0]),
        )[:8]

        LOGGER.info(
            "BACKTEST SYMBOL ANALYSIS COMPLETE | "
            "symbol=%s candidates=%d engine_calls=%d "
            "technical_accept=%d btc_reject=%d trades=%d "
            "engine_seconds=%.2f btc_seconds=%.2f "
            "simulation_seconds=%.2f total_seconds=%.2f "
            "top_rejects=%s",
            history.symbol,
            len(candidates),
            engine_calls,
            diagnostics["TECHNICAL_ACCEPT"],
            diagnostics["BTC_REJECT"],
            len(trades),
            engine_seconds,
            btc_seconds,
            simulation_seconds,
            time.monotonic() - symbol_started,
            reject_counts,
        )

        return trades

    async def run(
        self,
        days: int,
    ) -> BacktestSummary:

        days = int(days)

        if days not in {
            1,
            7,
            30,
            90,
        }:
            raise ValueError(
                "Supported backtests: "
                "1D, 7D, 30D, 90D"
            )

        if self._lock.locked():
            raise BacktestAlreadyRunning(
                "A backtest is already running. "
                "Please wait for it to finish."
            )

        async with self._lock:

            started = time.monotonic()

            stop_event = (
                asyncio.Event()
            )

            state: dict[
                str,
                Any,
            ] = {
                "phase": "INITIALIZING",
                "total": 0,
                "processed": 0,
                "tested": 0,
                "data_errors": 0,
                "engine_errors": 0,
                "simulation_errors": 0,
                "signals": 0,
                "worker": "-",
                "symbol": "-",
                "last_symbol_seconds": 0.0,
                "data_seconds": 0.0,
                "analysis_seconds": 0.0,
            }

            heartbeat = (
                asyncio.create_task(
                    self._heartbeat(
                        state,
                        started,
                        stop_event,
                        days,
                    ),
                    name=(
                        "backtest-heartbeat"
                    ),
                )
            )

            diagnostics: defaultdict[
                str,
                int,
            ] = defaultdict(int)

            trades: list[
                SimulatedTrade
            ] = []

            try:

                period_end = (
                    int(
                        time.time()
                        * 1000
                    )
                    // M5_MS
                    * M5_MS
                )

                period_start = (
                    period_end
                    - days
                    * 24
                    * 60
                    * 60
                    * 1000
                )

                state[
                    "phase"
                ] = "UNIVERSE"

                try:

                    symbols = list(
                        await asyncio.wait_for(
                            self.universe.refresh(),
                            timeout=60,
                        )
                    )[:MAX_BACKTEST_SYMBOLS]

                except Exception:

                    state[
                        "data_errors"
                    ] += 1

                    raise

                if not symbols:

                    raise RuntimeError(
                        "No eligible MEXC "
                        "Futures symbols are "
                        "available for backtesting."
                    )

                state[
                    "total"
                ] = len(symbols)

                LOGGER.info(
                    "BACKTEST START | "
                    "days=%d symbols=%d "
                    "start=%d end=%d",
                    days,
                    len(symbols),
                    period_start,
                    period_end,
                )

                state[
                    "phase"
                ] = "BTC DATA"

                try:

                    btc_history = (
                        await self._fetch_btc_history(
                            period_start,
                            period_end,
                        )
                    )

                except Exception:

                    state[
                        "data_errors"
                    ] += 1

                    diagnostics[
                        "BTC_DATA_ERROR"
                    ] += 1

                    raise

                btc_context_cache: dict = {}

                queue: asyncio.Queue[
                    str | None
                ] = asyncio.Queue()

                for symbol in symbols:
                    queue.put_nowait(
                        symbol
                    )

                for _ in range(
                    self.max_concurrency
                ):
                    queue.put_nowait(
                        None
                    )

                state_lock = asyncio.Lock()

                async def worker(
                    worker_id: int,
                ) -> None:

                    while True:

                        symbol = (
                            await queue.get()
                        )

                        try:

                            if symbol is None:
                                return

                            state[
                                "worker"
                            ] = worker_id

                            state[
                                "symbol"
                            ] = symbol

                            state[
                                "phase"
                            ] = "PREFILTER"

                            data_started = (
                                time.monotonic()
                            )

                            try:

                                if (
                                    str(
                                        symbol
                                    ).upper()
                                    == "BTC_USDT"
                                ):
                                    history = (
                                        btc_history
                                    )

                                else:

                                    history = (
                                        await self._prepare_symbol_history(
                                            symbol,
                                            period_start,
                                            period_end,
                                        )
                                    )

                                for (
                                    key,
                                    value,
                                ) in (
                                    history
                                    .diagnostics
                                    .items()
                                ):
                                    diagnostics[
                                        key
                                    ] += int(
                                        value
                                    )

                            except asyncio.CancelledError:
                                raise

                            except Exception as exc:

                                async with state_lock:
                                    state[
                                        "data_errors"
                                    ] += 1

                                diagnostics[
                                    (
                                        "DATA_ERROR_"
                                        f"{type(exc).__name__}"
                                    )
                                ] += 1

                                LOGGER.exception(
                                    "BACKTEST DATA ERROR | "
                                    "%s | %s",
                                    symbol,
                                    exc,
                                )

                                continue

                            data_seconds = (
                                time.monotonic()
                                - data_started
                            )

                            async with state_lock:

                                state[
                                    "data_seconds"
                                ] += data_seconds

                            state[
                                "phase"
                            ] = "ANALYSIS"

                            analysis_started = (
                                time.monotonic()
                            )

                            pre_analysis_diag = dict(
                                history.diagnostics
                            )

                            try:

                                (
                                    symbol_trades,
                                    symbol_diag,
                                ) = (
                                    await self._run_symbol_analysis_with_timeout(
                                        history,
                                        period_start,
                                        period_end,
                                        btc_history,
                                        state,
                                    )
                                )

                                for (
                                    key,
                                    value,
                                ) in (
                                    symbol_diag.items()
                                ):

                                    delta = (
                                        int(value)
                                        - int(
                                            pre_analysis_diag.get(
                                                key,
                                                0,
                                            )
                                        )
                                    )

                                    if delta > 0:
                                        diagnostics[
                                            key
                                        ] += delta

                            except asyncio.CancelledError:
                                raise

                            except Exception as exc:

                                async with state_lock:

                                    state[
                                        "engine_errors"
                                    ] += 1

                                diagnostics[
                                    (
                                        "ANALYSIS_ERROR_"
                                        f"{type(exc).__name__}"
                                    )
                                ] += 1

                                if isinstance(
                                    exc,
                                    TimeoutError,
                                ):

                                    diagnostics[
                                        "ANALYSIS_TIMEOUT"
                                    ] += 1

                                    LOGGER.error(
                                        "BACKTEST ANALYSIS "
                                        "TIMEOUT | %s | %s",
                                        symbol,
                                        exc,
                                    )

                                else:

                                    LOGGER.exception(
                                        "BACKTEST ANALYSIS "
                                        "ERROR | %s | %s",
                                        symbol,
                                        exc,
                                    )

                                continue

                            analysis_seconds = (
                                time.monotonic()
                                - analysis_started
                            )

                            async with state_lock:

                                state[
                                    "analysis_seconds"
                                ] += (
                                    analysis_seconds
                                )

                                state[
                                    "last_symbol_seconds"
                                ] = (
                                    data_seconds
                                    + analysis_seconds
                                )

                            trades.extend(
                                symbol_trades
                            )

                            async with state_lock:

                                state[
                                    "tested"
                                ] += 1

                                state[
                                    "signals"
                                ] = len(
                                    trades
                                )

                            LOGGER.info(
                                "BACKTEST PROGRESS | "
                                "days=%d "
                                "processed=%d/%d "
                                "tested=%d "
                                "data_errors=%d "
                                "engine_errors=%d "
                                "simulation_errors=%d "
                                "signals=%d",
                                days,
                                state[
                                    "processed"
                                ],
                                len(symbols),
                                state[
                                    "tested"
                                ],
                                state[
                                    "data_errors"
                                ],
                                state[
                                    "engine_errors"
                                ],
                                state[
                                    "simulation_errors"
                                ],
                                len(trades),
                            )

                        finally:

                            if symbol is not None:

                                async with state_lock:

                                    state[
                                        "processed"
                                    ] += 1

                            queue.task_done()

                workers = [
                    asyncio.create_task(
                        worker(index),
                        name=(
                            f"backtest-worker-{index}"
                        ),
                    )
                    for index
                    in range(
                        self.max_concurrency
                    )
                ]

                await asyncio.gather(
                    *workers
                )

                state[
                    "phase"
                ] = "FINALIZING"

                trades.sort(
                    key=lambda trade: (
                        trade.signal_time_ms
                    )
                )

                summary = summarize(
                    days=days,
                    coins_selected=(
                        len(symbols)
                    ),
                    coins_tested=int(
                        state["tested"]
                    ),
                    data_errors=int(
                        state["data_errors"]
                    ),
                    trades=trades,
                    diagnostics=diagnostics,
                )

                LOGGER.info(
                    "BACKTEST COMPLETE | "
                    "days=%d tested=%d "
                    "data_errors=%d "
                    "engine_errors=%d "
                    "simulation_errors=%d "
                    "signals=%d duration=%.2fs",
                    days,
                    state[
                        "tested"
                    ],
                    state[
                        "data_errors"
                    ],
                    state[
                        "engine_errors"
                    ],
                    state[
                        "simulation_errors"
                    ],
                    len(trades),
                    (
                        time.monotonic()
                        - started
                    ),
                )

                return summary

            finally:

                stop_event.set()

                heartbeat.cancel()

                await asyncio.gather(
                    heartbeat,
                    return_exceptions=True,
                )
