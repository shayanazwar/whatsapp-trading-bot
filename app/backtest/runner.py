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
    MIN_TRIGGER_BODY,
    MIN_TRIGGER_RVOL,
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

        LOGGER.info(
            "BACKTEST CHILD RESULT SEND START | symbol=%s trades=%d",
            history.symbol,
            len(symbol_trades),
        )
        conn.send(
            (
                "ok",
                symbol_trades,
                dict(history.diagnostics),
            )
        )
        LOGGER.info(
            "BACKTEST CHILD RESULT SENT | symbol=%s trades=%d",
            history.symbol,
            len(symbol_trades),
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
    backtest_15m_context: dict[str, Any] = field(default_factory=dict)


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

                    if message and message[0] in {"ok", "error"}:
                        LOGGER.info(
                            "BACKTEST PARENT RESULT RECEIVED | symbol=%s pid=%s type=%s",
                            history.symbol,
                            process.pid,
                            message[0],
                        )

                    if message and messa
