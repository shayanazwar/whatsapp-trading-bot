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
IPC_DRAIN_YIELD_EVERY = 100
IPC_IDLE_SLEEP_SECONDS = 0.05
IPC_TERMINAL_GRACE_SECONDS = 1.0


class BacktestAnalysisTimeout(TimeoutError):
    """Analysis subprocess exceeded its watchdog/parent deadline."""

    def __init__(self, message: str, *, last_stage: str = "UNKNOWN", last_progress: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.last_stage = last_stage
        self.last_progress = dict(last_progress or {})


class BacktestAnalysisProcessError(RuntimeError):
    """Isolated child returned a terminal error with its last known progress."""

    def __init__(self, message: str, *, last_progress: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.last_progress = dict(last_progress or {})


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
    # Compatibility name retained for existing callers. This is now the
    # authoritative causal candidate-discovery result, not a technical prefilter.
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


def _normalize_engine_reject_reason(reason: Any) -> str:
    raw = str(reason or "").strip()
    key = raw.lower().replace(" ", "_").replace("/", "_").replace("-", "_")
    mapping = {
        "4h_regime": "4H_REGIME",
        "1h_alignment": "1H_ALIGNMENT",
        "15m_bos": "15M_BOS",
        "15m_post_bos_retest": "15M_RETEST",
        "15m_directional_setup": "15M_DIRECTIONAL_SETUP",
        "15m_entry_confirmation": "15M_ENTRY",
        "5m_trigger_confirmation": "5M_TRIGGER",
        "momentum": "MOMENTUM",
        "volume_rvol": "VOLUME_RVOL",
        "target_path_location": "TARGET_PATH_LOCATION",
        "risk_rr": "RISK_RR",
        "volatility": "VOLATILITY",
        "score": "SCORE",
        "confirmation_families": "CONFIRMATION_FAMILIES",
        "technical_hard_gate": "TECHNICAL_HARD_GATE",
    }
    if key in mapping:
        return mapping[key]
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in raw.upper())
    safe = "_".join(part for part in safe.split("_") if part)
    return safe[:80] or "UNKNOWN"


def _log_gate_funnel(diagnostics: dict[str, int]) -> None:
    def g(key: str) -> int:
        try:
            return int(diagnostics.get(key, 0))
        except Exception:
            return 0

    LOGGER.info("=" * 72)
    LOGGER.info("BACKTEST GATE FUNNEL")
    LOGGER.info("=" * 72)
    LOGGER.info(
        "SYMBOLS | discovered=%d selected=%d data_ready=%d data_error=%d "
        "candidate_bearing=%d analysis_completed=%d analysis_failed=%d",
        g("SYMBOLS_DISCOVERED"), g("SYMBOLS_SELECTED"),
        g("SYMBOLS_DATA_READY"), g("SYMBOLS_DATA_ERROR"),
        g("SYMBOLS_WITH_CANDIDATES"), g("SYMBOLS_ANALYSIS_COMPLETED"),
        g("SYMBOLS_ANALYSIS_FAILED"),
    )
    LOGGER.info(
        "CANDIDATES | discovered=%d unique_timestamps=%d engine_evaluated=%d "
        "warmup_reject=%d duplicate_timestamp=%d overlap_skipped=%d",
        g("CANDIDATES_DISCOVERED"), g("CANDIDATES_UNIQUE_TIMESTAMPS"),
        g("CANDIDATES_ENGINE_EVALUATED"), g("ENGINE_WARMUP_REJECT"),
        g("DUPLICATE_TIMESTAMP_SKIPPED"), g("OVERLAPPING_SIGNAL_SKIPPED"),
    )
    LOGGER.info(
        "ACCOUNTING | engine_gap=%d technical_gap=%d",
        max(0, g("ENGINE_CALLS") - g("ENGINE_SUCCESS") - g("ENGINE_ERRORS") - g("ENGINE_OUTCOME_UNKNOWN_CALLS")),
        g("TECHNICAL_ACCOUNTING_GAP"),
    )
    LOGGER.info(
        "ENGINE | calls=%d success=%d errors=%d unknown_calls=%d timeouts=%d",
        g("ENGINE_CALLS"), g("ENGINE_SUCCESS"), g("ENGINE_ERRORS"),
        g("ENGINE_OUTCOME_UNKNOWN_CALLS"), g("ANALYSIS_TIMEOUT"),
    )
    LOGGER.info(
        "TECHNICAL | accepted=%d rejected=%d structure_duplicates=%d invalid_side=%d "
        "BTC_accept=%d BTC_reject=%d BTC_errors=%d",
        g("TECHNICAL_ACCEPT"), g("TECHNICAL_REJECT"),
        g("DUPLICATE_STRUCTURE_SKIPPED"), g("INVALID_ENGINE_SIDE"),
        g("BTC_ACCEPT"), g("BTC_REJECT"), g("BTC_CONTEXT_ERRORS"),
    )
    LOGGER.info(
        "SIMULATION | accepted=%d no_trade=%d errors=%d no_future=%d TP2=%d SL=%d expired=%d",
        g("SIMULATION_ACCEPT"), g("SIMULATION_NO_TRADE"),
        g("SIMULATION_ERRORS"), g("NO_FUTURE_CANDLES"),
        g("OUTCOME_TP2"), g("OUTCOME_SL"), g("EXPIRY"),
    )
    reject_keys = sorted(
        ((key, int(value)) for key, value in diagnostics.items()
         if key.startswith("ENGINE_REJECT_") and int(value) > 0),
        key=lambda item: (-item[1], item[0]),
    )
    if reject_keys:
        LOGGER.info("TECHNICAL REJECT BUCKETS (one candidate may fail multiple gates):")
        for key, value in reject_keys[:30]:
            LOGGER.info("  %s=%d", key[len("ENGINE_REJECT_"):], value)
    else:
        LOGGER.info("TECHNICAL REJECT BUCKETS: none")
    LOGGER.info("=" * 72)


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
                    "engine_errors=%d analysis_errors=%d "
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
                    state.get("analysis_errors", 0),
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
                    protocol=p
