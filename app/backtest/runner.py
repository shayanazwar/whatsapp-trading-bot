from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import pickle
import tempfile
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
    MIN_TRIGGER_BODY,
    MIN_TRIGGER_RVOL,
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
MAX_SYMBOL_CONCURRENCY = 1
SYMBOL_FETCH_TIMEOUT_SECONDS = 120

# Emergency ceiling only. Normal symbols should finish far sooner.
SYMBOL_ANALYSIS_TIMEOUT_SECONDS = 180

# Startup/import/pickle phase should never hold a worker indefinitely.
# Normal child startup is much faster than this on the deployed runtime.
CHILD_BOOT_TIMEOUT_SECONDS = 45

HEARTBEAT_INTERVAL_SECONDS = 30

CHILD_PROGRESS_INTERVAL_CALLS = 25
CHILD_PROGRESS_INTERVAL_SECONDS = 20.0

MIN_4H_WARMUP_MS = 45 * 24 * 60 * 60 * 1000
MIN_1H_WARMUP_MS = 21 * 24 * 60 * 60 * 1000
MIN_15M_WARMUP_MS = 7 * 24 * 60 * 60 * 1000
MIN_5M_WARMUP_MS = 2 * 24 * 60 * 60 * 1000
MIN_1D_WARMUP_MS = 60 * 24 * 60 * 60 * 1000

MAX_SETUP_AGE_15M = 8


class BacktestAlreadyRunning(RuntimeError):
    pass


class BacktestDataError(RuntimeError):
    """Historical-data failure isolated from engine/analysis failures."""

    pass



def _child_send(conn: Any, payload: tuple[Any, ...]) -> None:
    """Best-effort status/result send from the isolated child."""
    try:
        conn.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        # Parent may already have timed out and closed the pipe.
        pass
    except Exception:
        # Never let diagnostics/IPC prevent child cleanup.
        try:
            LOGGER.exception("BACKTEST CHILD IPC SEND FAILED")
        except Exception:
            pass


def _isolated_backtest_symbol(
    payload_path: str,
    start: int,
    end: int,
    fee_rate: float,
    slippage_bps: float,
    default_max_hold_minutes: float,
    mexc_api_base_url: str,
    conn: Any,
    child_done: Any,
) -> None:
    """Fetch, candidate-build, analyze, and simulate one symbol in one child loop.

    Critical lifecycle rule:
        The MEXC AsyncClient is created, used, and closed inside the SAME
        asyncio.run() event loop. It is never allowed to outlive that loop.

    This child contains the entire expensive symbol lifecycle so the parent
    Render/asyncio event loop remains responsive.
    """
    pid = multiprocessing.current_process().pid
    _child_send(conn, ("status", "BOOT", int(pid or 0)))

    async def _child_async_main() -> tuple[Any, ...]:
        _child_send(conn, ("status", "PICKLE_LOAD_START", int(pid or 0)))
        with open(payload_path, "rb") as handle:
            symbol, btc_history = pickle.load(handle)
        symbol = str(symbol)
        _child_send(
            conn,
            ("status", "PICKLE_LOAD_DONE", int(pid or 0), symbol),
        )

        LOGGER.info(
            "BACKTEST CHILD START | symbol=%s pid=%s",
            symbol,
            pid,
        )

        _child_send(
            conn,
            ("status", "RUNNER_INIT_START", int(pid or 0), symbol),
        )

        settings = SimpleNamespace(
            backtest_fee_rate=fee_rate,
            backtest_slippage_bps=slippage_bps,
            backtest_max_holding_minutes=default_max_hold_minutes,
            mexc_api_base_url=mexc_api_base_url,
        )

        _child_send(
            conn,
            ("status", "RUNNER_INIT_DONE", int(pid or 0), symbol),
        )

        # --------------------------------------------------------------
        # FETCH + CANDIDATE GENERATION
        # --------------------------------------------------------------
        _child_send(
            conn,
            ("status", "FETCH_START", int(pid or 0), symbol),
        )

        client = MexcClient(settings)
        fetch_runner = BacktestRunner(
            client=client,
            universe=None,
            settings=settings,
            max_concurrency=1,
        )

        try:
            history = await fetch_runner._prepare_symbol_history(
                symbol,
                start,
                end,
            )
        except BaseException as exc:
            LOGGER.exception(
                "BACKTEST CHILD DATA ERROR | symbol=%s | type=%s message=%s",
                symbol,
                type(exc).__name__,
                exc,
            )
            return (
                "data_error",
                type(exc).__name__,
                str(exc),
            )
        finally:
            # The close happens INSIDE the same event loop in which the client
            # was created and used. A close failure is diagnostic only and must
            # never convert successfully fetched data into an analysis error.
            try:
                await client.close()
            except BaseException as close_exc:
                LOGGER.exception(
                    "BACKTEST CHILD CLIENT CLOSE FAILED | symbol=%s | pid=%s | type=%s message=%s",
                    symbol,
                    pid,
                    type(close_exc).__name__,
                    close_exc,
                )

        _child_send(
            conn,
            (
                "status",
                "FETCH_DONE",
                int(pid or 0),
                symbol,
                len(getattr(history, "prefilter_candidates", ()) or ()),
            ),
        )

        # --------------------------------------------------------------
        # SYNCHRONOUS CANDIDATE GENERATION + ENGINE + SIMULATION
        # --------------------------------------------------------------
        _child_send(
            conn,
            ("status", "PREFILTER_ANALYSIS_START", int(pid or 0), symbol),
        )

        analysis_runner = BacktestRunner(
            client=None,
            universe=None,
            settings=settings,
            max_concurrency=1,
        )

        started = time.monotonic()
        try:
            symbol_trades = analysis_runner._backtest_symbol(
                history,
                start,
                end,
                btc_history,
                {},
            )
        except BaseException as exc:
            LOGGER.exception(
                "BACKTEST CHILD ANALYSIS ERROR | symbol=%s | type=%s message=%s",
                symbol,
                type(exc).__name__,
                exc,
            )
            return (
                "error",
                type(exc).__name__,
                str(exc),
            )

        elapsed = time.monotonic() - started

        _child_send(
            conn,
            (
                "status",
                "ANALYSIS_DONE",
                int(pid or 0),
                symbol,
                float(elapsed),
                len(symbol_trades),
            ),
        )

        LOGGER.info(
            "BACKTEST CHILD COMPLETE | symbol=%s trades=%d seconds=%.2f",
            symbol,
            len(symbol_trades),
            elapsed,
        )

        return (
            "ok",
            symbol_trades,
            dict(history.diagnostics),
        )

    try:
        result = asyncio.run(_child_async_main())
        kind = result[0]

        if kind == "data_error":
            _child_send(
                conn,
                (
                    "data_error",
                    result[1],
                    result[2],
                ),
            )
        elif kind == "error":
            _child_send(
                conn,
                (
                    "error",
                    result[1],
                    result[2],
                ),
            )
        elif kind == "ok":
            _child_send(
                conn,
                (
                    "ok",
                    result[1],
                    result[2],
                ),
            )
        else:
            _child_send(
                conn,
                (
                    "error",
                    "InvalidChildResult",
                    repr(result),
                ),
            )
    except BaseException as exc:
        LOGGER.exception(
            "BACKTEST CHILD FATAL ERROR | type=%s message=%s",
            type(exc).__name__,
            exc,
        )
        _child_send(
            conn,
            (
                "error",
                type(exc).__name__,
                str(exc),
            ),
        )
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


def _precompute_rsi_rvol(
    candles: list,
    period: int = 14,
    lookback: int = 20,
) -> tuple[list[float], list[float]]:
    """Precompute RSI and RVOL once for the conservative candidate prefilter."""
    n = len(candles)
    rsi_values = [50.0] * n
    rvol_values = [0.0] * n
    if n == 0:
        return rsi_values, rvol_values

    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]

    if n >= period + 1:
        gains = [0.0] * (n - 1)
        losses = [0.0] * (n - 1)
        for i in range(1, n):
            change = closes[i] - closes[i - 1]
            gains[i - 1] = max(change, 0.0)
            losses[i - 1] = max(-change, 0.0)

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        def current_rsi() -> float:
            if avg_loss == 0.0:
                return 100.0
            rs = avg_gain / avg_loss
            return 100.0 - (100.0 / (1.0 + rs))

        rsi_values[period] = current_rsi()
        for i in range(period + 1, n):
            avg_gain = ((avg_gain * (period - 1)) + gains[i - 1]) / period
            avg_loss = ((avg_loss * (period - 1)) + losses[i - 1]) / period
            rsi_values[i] = current_rsi()

    if n >= lookback + 1:
        rolling = sum(volumes[:lookback])
        for i in range(lookback, n):
            average = rolling / lookback
            rvol_values[i] = volumes[i] / average if average > 0.0 else 0.0
            rolling += volumes[i]
            rolling -= volumes[i - lookback]

    return rsi_values, rvol_values


def _fast_directional_entry_prefilter(
    candles: list,
    index: int,
    side: str,
    setup_level: Any,
    retest_time: Any,
    rsi_values: list[float],
    rvol_values: list[float],
    *,
    require_5m: bool = False,
) -> bool:
    """Conservative O(1) necessary-condition check; engine stays authoritative."""
    if side not in {"LONG", "SHORT"} or setup_level is None:
        return False
    if index < 1 or index >= len(candles):
        return False

    cur = candles[index]
    o = float(cur["open"])
    h = float(cur["high"])
    l = float(cur["low"])
    close = float(cur["close"])
    rng = max(h - l, 1e-12)
    body = abs(close - o) / rng
    r = float(rsi_values[index])
    rv = float(rvol_values[index])
    level = float(setup_level)

    if not require_5m and retest_time is not None:
        if int(cur["time"]) <= int(retest_time):
            return False

    close_location = (close - l) / rng if side == "LONG" else (h - close) / rng
    if body < float(MIN_TRIGGER_BODY) or rv < float(MIN_TRIGGER_RVOL) or close_location < 0.70:
        return False

    if side == "LONG":
        return close > level and close > o and r >= 55.0
    return close < level and close < o and r <= 45.0


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
                    time.monotonic()
                    - started,
                )

    async def _run_symbol_analysis_with_timeout(
        self,
        symbol: str,
        start: int,
        end: int,
        btc_history: SymbolHistory,
    ) -> tuple[list[SimulatedTrade], dict[str, int]]:
        """Run one symbol in an isolated subprocess with hard child timeouts.

        There are two independent failure classes:
        1. CHILD_BOOT_TIMEOUT_SECONDS covers spawn/import/pickle/initialization
           until the child runner is fully initialized.
        2. SYMBOL_ANALYSIS_TIMEOUT_SECONDS covers the full symbol analysis.

        The parent owns the watchdog here. This keeps timeout handling in the
        asyncio task that already owns the child process and removes the old
        thread-based watchdog race.
        """
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

        def _terminate_process(reason: str) -> None:
            """Best-effort terminate/kill with clear logging."""
            if process is None:
                return

            if process.is_alive():
                LOGGER.error(
                    "BACKTEST CHILD TERMINATING | symbol=%s | pid=%s | reason=%s",
                    symbol,
                    process.pid,
                    reason,
                )

                try:
                    process.terminate()
                except Exception:
                    LOGGER.exception(
                        "BACKTEST CHILD TERMINATE FAILED | symbol=%s | pid=%s",
                        symbol,
                        process.pid,
                    )
                    return

                try:
                    process.join(3.0)
                except Exception:
                    LOGGER.exception(
                        "BACKTEST CHILD JOIN FAILED | symbol=%s | pid=%s",
                        symbol,
                        process.pid,
                    )

                if process.is_alive():
                    try:
                        process.kill()
                    except Exception:
                        LOGGER.exception(
                            "BACKTEST CHILD KILL FAILED | symbol=%s | pid=%s",
                            symbol,
                            process.pid,
                        )

                    try:
                        process.join(2.0)
                    except Exception:
                        LOGGER.exception(
                            "BACKTEST CHILD FINAL JOIN FAILED | symbol=%s | pid=%s",
                            symbol,
                            process.pid,
                        )

            LOGGER.error(
                "BACKTEST CHILD TERMINATED | symbol=%s | pid=%s | exitcode=%s | reason=%s",
                symbol,
                process.pid,
                process.exitcode,
                reason,
            )

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f"pta-backtest-{symbol}-",
                suffix=".pkl",
                delete=False,
            ) as handle:
                payload_path = handle.name
                pickle.dump(
                    (symbol, btc_history),
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
                handle.flush()

            mexc_api_base_url = str(
                getattr(
                    self.client,
                    "base_url",
                    getattr(
                        self.settings,
                        "mexc_api_base_url",
                        "https://api.mexc.com",
                    ),
                )
            )

            process = ctx.Process(
                target=_isolated_backtest_symbol,
                args=(
                    payload_path,
                    start,
                    end,
                    fee_rate,
                    slippage_bps,
                    default_max_hold_minutes,
                    mexc_api_base_url,
                    child_conn,
                    child_done,
                ),
                name=f"backtest-analysis-{symbol}",
            )
            process.daemon = True

            payload_kb = (
                os.path.getsize(payload_path) / 1024.0
                if payload_path
                else 0.0
            )

            LOGGER.info(
                "BACKTEST ANALYSIS PREP DEFERRED | symbol=%s | fetch+candidate-generation=child",
                symbol,
            )
            LOGGER.info(
                "BACKTEST ANALYSIS PROCESS START | symbol=%s | payload_kb=%.1f",
                symbol,
                payload_kb,
            )

            # Keep process.start off the asyncio event loop.
            await asyncio.to_thread(process.start)

            try:
                child_conn.close()
            except Exception:
                pass

            LOGGER.info(
                "BACKTEST ANALYSIS PROCESS STARTED | symbol=%s | pid=%s | method=spawn | payload=FILE",
                symbol,
                process.pid,
            )

            analysis_started = time.monotonic()
            boot_deadline = (
                analysis_started + CHILD_BOOT_TIMEOUT_SECONDS
            )
            analysis_deadline = (
                analysis_started + SYMBOL_ANALYSIS_TIMEOUT_SECONDS
            )

            child_ready = False
            child_phase = "PROCESS_STARTED"
            payload: tuple[Any, ...] | None = None

            while True:
                now = time.monotonic()

                # Drain every status/result message the child has produced.
                while parent_conn.poll(0):
                    try:
                        message = parent_conn.recv()
                    except (EOFError, OSError):
                        message = None

                    if message is None:
                        break

                    if not isinstance(message, tuple) or not message:
                        LOGGER.warning(
                            "BACKTEST CHILD INVALID MESSAGE | symbol=%s | message=%r",
                            symbol,
                            message,
                        )
                        continue

                    kind = message[0]

                    if kind == "status":
                        status = (
                            str(message[1])
                            if len(message) > 1
                            else "UNKNOWN"
                        )
                        child_phase = status

                        if status == "RUNNER_INIT_DONE":
                            child_ready = True

                        LOGGER.info(
                            "BACKTEST CHILD STATUS | symbol=%s | pid=%s | phase=%s",
                            symbol,
                            process.pid,
                            status,
                        )
                        continue

                    payload = message
                    break

                if payload is not None:
                    break

                if not child_ready and now >= boot_deadline:
                    LOGGER.error(
                        "BACKTEST CHILD BOOT TIMEOUT | symbol=%s | pid=%s | timeout=%ss | phase=%s",
                        symbol,
                        process.pid,
                        CHILD_BOOT_TIMEOUT_SECONDS,
                        child_phase,
                    )
                    _terminate_process("child_boot_timeout")

                    raise TimeoutError(
                        f"{symbol}: child failed to reach "
                        f"BACKTEST CHILD BOOT within "
                        f"{CHILD_BOOT_TIMEOUT_SECONDS}s "
                        f"(phase={child_phase})"
                    )

                if now >= analysis_deadline:
                    LOGGER.error(
                        "BACKTEST WATCHDOG TIMEOUT | symbol=%s | pid=%s | timeout=%ss | phase=%s",
                        symbol,
                        process.pid,
                        SYMBOL_ANALYSIS_TIMEOUT_SECONDS,
                        child_phase,
                    )
                    _terminate_process("symbol_analysis_timeout")

                    raise TimeoutError(
                        f"{symbol}: analysis exceeded "
                        f"{SYMBOL_ANALYSIS_TIMEOUT_SECONDS}s "
                        f"(phase={child_phase})"
                    )

                if not process.is_alive():
                    # The child may have exited immediately after sending its
                    # final message, so do one final non-blocking drain.
                    if parent_conn.poll(0.05):
                        try:
                            payload = parent_conn.recv()
                        except (EOFError, OSError):
                            payload = None

                    if payload is not None:
                        break

                    raise RuntimeError(
                        f"{symbol}: analysis subprocess exited "
                        f"without a result "
                        f"(exitcode={process.exitcode}, phase={child_phase})"
                    )

                await asyncio.sleep(0.10)

            if payload is None:
                raise RuntimeError(
                    f"{symbol}: analysis subprocess returned no payload"
                )

            if payload[0] == "data_error":
                raise BacktestDataError(
                    f"{symbol}: historical data preparation failed: "
                    f"{payload[1]}: {payload[2]}"
                )

            if payload[0] == "error":
                raise RuntimeError(
                    f"{symbol}: isolated analysis failed: "
                    f"{payload[1]}: {payload[2]}"
                )

            if payload[0] != "ok":
                raise RuntimeError(
                    f"{symbol}: invalid analysis subprocess payload: "
                    f"{payload[0]!r}"
                )

            return payload[1], payload[2]

        except asyncio.CancelledError:
            if process is not None:
                _terminate_process("parent_task_cancelled")
            raise

        finally:
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
                    _terminate_process("parent_cleanup")

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
                        symbol,
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
    def _find_15m_setup_windows(
        c15: list,
        period_start: int,
        period_end: int,
        diagnostics: dict[str, int],
        *,
        context: dict[str, Any] | None = None,
    ) -> tuple[
        tuple[int, Any],
        ...
    ]:
        """Build candidates with the same causal BOS/retest rules as the engine.

        Important execution rule: this function is CPU-bound and must run only
        inside the isolated backtest child. It is never called from the parent
        asyncio worker during normal symbol preparation.

        The engine's authoritative selection semantics are preserved exactly:
          1. BOS events are computed causally from confirmed swings.
          2. At candidate prefix index ``i``, only BOS events in the last 70
             candles are visible.
          3. The newest BOS is selected only when its first valid retest has
             already occurred and both BOS/retest age limits are satisfied.

        ``context`` is the engine's reusable 15M backtest context so BOS/ATR/swing
        calculations are performed once per symbol and reused by both candidate
        generation and every authoritative engine call.
        """
        if not c15:
            return ()

        if context is None:
            context = _build_15m_backtest_context(c15)

        bos_by_side = {
            "LONG": list(context.get("bos_long") or []),
            "SHORT": list(context.get("bos_short") or []),
        }

        candidates: dict[tuple[int, str], dict[str, Any]] = {}

        # First compute the first valid retest for every BOS exactly once.
        setup_by_side: dict[str, list[dict[str, Any]]] = {}
        setup_indices: dict[str, list[int]] = {}

        for side in ("LONG", "SHORT"):
            setups: list[dict[str, Any]] = []
            bos_events = bos_by_side[side]

            diagnostics[f"BOS_{side}"] += len(bos_events)

            for bos in bos_events:
                retest = _pullback_retest(
                    c15,
                    side,
                    bos,
                    MAX_SETUP_AGE_15M,
                )
                if not retest.get("valid"):
                    continue

                diagnostics[f"RETEST_{side}"] += 1

                setups.append(
                    {
                        "bos_index": int(bos["index"]),
                        "bos_time": int(bos["time"]),
                        "bos_level": float(bos["level"]),
                        "bos_strength": _safe_float(bos.get("strength"), 0.0),
                        "retest_index": int(retest["index"]),
                        "retest_time": int(retest["time"]),
                        "retest_quality": _safe_float(retest.get("quality"), 0.0),
                        "retest_rejection": bool(retest.get("rejection")),
                    }
                )

            setups.sort(key=lambda item: (
                int(item["bos_index"]),
                int(item["retest_index"]),
            ))
            setup_by_side[side] = setups
            setup_indices[side] = [int(item["bos_index"]) for item in setups]

        open_times = [_row_time(row) for row in c15]
        max_bos_age = MAX_SETUP_AGE_15M + 2

        # The engine evaluates only closed 15M candles and uses candle-index age
        # for BOS/retest freshness. Use the same exact index semantics here rather
        # than timestamp deltas so gaps in historical data cannot change candidate
        # eligibility relative to analyze_candles().
        for index, open_time in enumerate(open_times):
            close_time = int(open_time) + M15_MS

            if close_time < period_start or close_time > period_end:
                continue

            for side in ("LONG", "SHORT"):
                setups = setup_by_side[side]
                indices = setup_indices[side]
                if not setups:
                    continue

                # Only a maximum of ``MAX_SETUP_AGE_15M + 1`` BOS positions can
                # possibly survive the age gate, so this backward search is bounded
                # and avoids repeatedly walking the complete setup list.
                pos = bisect_right(indices, index) - 1
                active_setup: dict[str, Any] | None = None

                while pos >= 0:
                    setup = setups[pos]
                    bos_index = int(setup["bos_index"])
                    retest_index = int(setup["retest_index"])

                    bos_age = index - bos_index
                    if bos_age < 0:
                        pos -= 1
                        continue
                    if bos_age > max_bos_age:
                        break

                    if retest_index <= index:
                        retest_age = index - retest_index
                        if 0 <= retest_age <= MAX_SETUP_AGE_15M:
                            active_setup = setup
                            break

                    pos -= 1

                if active_setup is None:
                    continue

                candidates[(close_time, side)] = {
                    "side": side,
                    "bos_level": float(active_setup["bos_level"]),
                    "bos_time": int(active_setup["bos_time"]),
                    "bos_strength": float(active_setup["bos_strength"]),
                    "retest_time": int(active_setup["retest_time"]),
                    "retest_index": int(active_setup["retest_index"]),
                    "retest_quality": float(active_setup["retest_quality"]),
                    "retest_rejection": bool(active_setup["retest_rejection"]),
                }

        diagnostics["SETUP_WINDOWS_15M"] += len(candidates)

        return tuple(
            (timestamp, meta)
            for (timestamp, _side), meta in sorted(
                candidates.items(),
                key=lambda item: (item[0][0], item[0][1]),
            )
        )

    async def _prepare_symbol_history(
        self,
        symbol: str,
        start: int,
        end: int,
    ) -> SymbolHistory:
        """Fetch one symbol's complete historical payload without CPU-bound analysis.

        Candidate generation is deliberately deferred to the isolated analysis
        child. The parent event loop therefore remains responsive throughout
        preparation, and the child watchdog can enforce a hard upper bound on the
        complete CPU workload.
        """
        starts = {
            "4h": start - MIN_4H_WARMUP_MS,
            "1h": start - MIN_1H_WARMUP_MS,
            "15m": start - MIN_15M_WARMUP_MS,
            "5m": start - MIN_5M_WARMUP_MS,
            "1d": start - MIN_1D_WARMUP_MS,
        }

        tasks = [
            asyncio.create_task(_fetch_timeframe(
                self.client, symbol, "4H", INTERVALS["4h"], starts["4h"], end
            )),
            asyncio.create_task(_fetch_timeframe(
                self.client, symbol, "1H", INTERVALS["1h"], starts["1h"], end
            )),
            asyncio.create_task(_fetch_timeframe(
                self.client, symbol, "15M", INTERVALS["15m"], starts["15m"], end
            )),
            asyncio.create_task(_fetch_timeframe(
                self.client, symbol, "5M", INTERVALS["5m"], starts["5m"], end
            )),
            asyncio.create_task(_fetch_timeframe(
                self.client, symbol, "1D", INTERVALS["1d"], starts["1d"], end
            )),
        ]

        try:
            c4_raw, c1_raw, c15_raw, c5_raw, c1d_raw = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=SYMBOL_FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # Keep the parent-side validation cheap and causal; authoritative engine
        # data-quality checks still happen on each point-in-time analysis prefix.
        c4 = [c for c in convert_candles(c4_raw) if int(c["time"]) + H4_MS <= end]
        c1 = [c for c in convert_candles(c1_raw) if int(c["time"]) + H1_MS <= end]
        c15 = [c for c in convert_candles(c15_raw) if int(c["time"]) + M15_MS <= end]

        if len(c4) < 205:
            raise ValueError(f"{symbol}: insufficient 4H candles ({len(c4)} < 205)")
        if len(c1) < 205:
            raise ValueError(f"{symbol}: insufficient 1H candles ({len(c1)} < 205)")
        if len(c15) < 80:
            raise ValueError(f"{symbol}: insufficient 15M candles ({len(c15)} < 80)")

        diagnostics: dict[str, int] = defaultdict(int)
        diagnostics["FIVE_MIN_FETCH"] += 1
        diagnostics["ONE_D_FETCH"] += 1
        diagnostics["CANDIDATES_DEFERRED_TO_CHILD"] += 1

        # ``prefilter_candidates`` is intentionally empty. The isolated child will
        # build them from the reusable 15M engine context and then feed that exact
        # context into analyze_candles().
        return SymbolHistory(
            symbol,
            c4_raw,
            c1_raw,
            c15_raw,
            c5_raw,
            c1d_raw,
            (),
            dict(diagnostics),
        )

    def _backtest_symbol(
        self,
        history: SymbolHistory,
        start: int,
        end: int,
        btc_history: SymbolHistory,
        btc_context_cache: dict | None = None,
    ) -> list[SimulatedTrade]:
        """Backtest one symbol using the authoritative engine as the sole signal gate.

        The Runner supplies point-in-time BOS/retest candidates and applies only
        conservative necessary-condition prefilters for 15M/5M entry. All
        authoritative signal decisions belong to analyze_candles().
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
        # Reuse point-in-time engine calculations whose cache keys prove that
        # the underlying closed candle set is identical across candidates.
        # This preserves the engine logic while avoiding repeated 4H/1H work.
        # Build the expensive 15M context exactly once. Candidate generation and
        # all authoritative engine calls consume this same context, preventing the
        # old duplicate BOS/ATR/swing pass.
        backtest_15m_context = _build_15m_backtest_context(c15)
        engine_cache: dict[Any, Any] = {
            "_BACKTEST_15M": backtest_15m_context,
        }

        if not candidates:
            local_diag = defaultdict(int)
            candidates = self._find_15m_setup_windows(
                c15,
                start,
                end,
                local_diag,
                context=backtest_15m_context,
            )
            for key, value in local_diag.items():
                history.diagnostics[key] = (
                    int(history.diagnostics.get(key, 0)) + int(value)
                )
            diagnostics["CANDIDATES_GENERATED_IN_CHILD"] = len(candidates)

        candidate_hints_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
        prefilter_rsi_15m, prefilter_rvol_15m = _precompute_rsi_rvol(c15)
        prefilter_rsi_5m, prefilter_rvol_5m = _precompute_rsi_rvol(c5)
        c15_times = [_row_time(row) for row in c15]
        c5_times = [_row_time(row) for row in c5]
        for candidate_time, candidate_hint in candidates:
            candidate_hints_by_time[int(candidate_time)].append(
                candidate_hint if isinstance(candidate_hint, dict) else {}
            )
        last_progress = time.monotonic()

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
            # evidence, so run the full engine only once per timestamp. The cheap
            # prefilter below checks every side hint for that timestamp so a LONG
            # hint can never suppress a valid SHORT engine result (or vice versa).
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

            # Fast point-in-time prefilters. These only test conditions that are
            # mathematically necessary for the authoritative 15M/5M engine gates.
            # All final signal decisions still come exclusively from analyze_candles().
            try:
                c15_index = bisect_right(
                    c15_times,
                    signal_close_time - M15_MS,
                ) - 1
                c5_index = bisect_right(
                    c5_times,
                    signal_close_time - M5_MS,
                ) - 1

                prefilter_ready = False
                for setup_hint in candidate_hints_by_time.get(
                    signal_close_time,
                    [_setup_hint],
                ):
                    setup_side = str(
                        (setup_hint or {}).get("side") or ""
                    ).upper()
                    setup_level = (setup_hint or {}).get("bos_level")
                    retest_time = (setup_hint or {}).get("retest_time")

                    if not _fast_directional_entry_prefilter(
                        c15,
                        c15_index,
                        setup_side,
                        setup_level,
                        retest_time,
                        prefilter_rsi_15m,
                        prefilter_rvol_15m,
                        require_5m=False,
                    ):
                        continue

                    if not _fast_directional_entry_prefilter(
                        c5,
                        c5_index,
                        setup_side,
                        setup_level,
                        retest_time,
                        prefilter_rsi_5m,
                        prefilter_rvol_5m,
                        require_5m=True,
                    ):
                        continue

                    prefilter_ready = True
                    break

                if not prefilter_ready:
                    diagnostics["PREFILTER_REJECT"] += 1
                    continue
            except Exception:
                diagnostics["PREFILTER_ERRORS"] += 1
                LOGGER.exception(
                    "BACKTEST PREFILTER FAILED | symbol=%s | candidate=%d/%d | signal=%d",
                    history.symbol,
                    candidate_index,
                    len(candidates),
                    signal_close_time,
                )
                continue

            engine_started = time.monotonic()

            print(
                f"BACKTEST ENGINE CALL START | symbol={history.symbol} "
                f"candidate={candidate_index}/{len(candidates)} signal={signal_close_time}",
                flush=True,
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
                    cache=engine_cache,
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

            print(
                f"BACKTEST ENGINE CALL DONE | symbol={history.symbol} "
                f"candidate={candidate_index}/{len(candidates)} "
                f"seconds={elapsed_engine:.3f}",
                flush=True,
            )

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
            7,
            30,
            90,
        }:
            raise ValueError(
                "Supported backtests: "
                "7D, 30D, 90D"
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
                    )[:300]

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
                        symbol = await queue.get()
                        try:
                            if symbol is None:
                                return

                            state["worker"] = worker_id
                            state["symbol"] = symbol
                            state["phase"] = "ISOLATED_FETCH+ANALYSIS"

                            symbol_started = time.monotonic()

                            try:
                                symbol_trades, symbol_diag = await self._run_symbol_analysis_with_timeout(
                                    str(symbol),
                                    period_start,
                                    period_end,
                                    btc_history,
                                )

                                for key, value in symbol_diag.items():
                                    diagnostics[key] += int(value)

                                state["simulation_errors"] += int(
                                    symbol_diag.get("SIMULATION_ERRORS", 0)
                                )

                            except asyncio.CancelledError:
                                raise

                            except BacktestDataError as exc:
                                state["data_errors"] += 1
                                diagnostics[f"DATA_ERROR_{type(exc).__name__}"] += 1
                                LOGGER.error(
                                    "BACKTEST DATA ERROR | %s | %s",
                                    symbol,
                                    exc,
                                )
                                continue

                            except Exception as exc:
                                state["engine_errors"] += 1
                                diagnostics[f"ANALYSIS_ERROR_{type(exc).__name__}"] += 1
                                if isinstance(exc, TimeoutError):
                                    diagnostics["ANALYSIS_TIMEOUT"] += 1
                                    LOGGER.error(
                                        "BACKTEST ANALYSIS TIMEOUT | %s | %s",
                                        symbol,
                                        exc,
                                    )
                                else:
                                    LOGGER.exception(
                                        "BACKTEST ANALYSIS ERROR | %s | %s",
                                        symbol,
                                        exc,
                                    )
                                continue

                            symbol_seconds = time.monotonic() - symbol_started
                            state["analysis_seconds"] += symbol_seconds
                            state["last_symbol_seconds"] = symbol_seconds
                            trades.extend(symbol_trades)
                            state["tested"] += 1
                            state["signals"] = len(trades)

                            LOGGER.info(
                                "BACKTEST PROGRESS | days=%d processed=%d/%d tested=%d "
                                "data_errors=%d engine_errors=%d simulation_errors=%d signals=%d",
                                days,
                                state["processed"],
                                len(symbols),
                                state["tested"],
                                state["data_errors"],
                                state["engine_errors"],
                                state["simulation_errors"],
                                len(trades),
                            )

                        finally:
                            async with state_lock:
                                state["processed"] += 1
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
