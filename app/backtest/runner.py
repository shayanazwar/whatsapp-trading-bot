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
            ("status", "ANALYSIS_CPU_START", int(pid or 0), symbol),
        )

        analysis_runner = BacktestRunner(
            client=None,
            universe=None,
            settings=settings,
            max_concurrency=1,
        )

        started = time.monotonic()
        try:
            def _analysis_progress(stage: str, detail: Any = None) -> None:
                _child_send(
                    conn,
                    (
                        "status",
                        str(stage),
                        int(pid or 0),
                        symbol,
                        detail,
                    ),
                )

            symbol_trades = analysis_runner._backtest_symbol(
                history,
                start,
                end,
                btc_history,
                {},
                progress_callback=_analysis_progress,
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
            ("status", "ANALYSIS_CPU_START", int(pid or 0), symbol),
        )

        analysis_runner = BacktestRunner(
            client=None,
            universe=None,
            settings=settings,
            max_concurrency=1,
        )

        started = time.monotonic()
        try:
            def _analysis_progress(stage: str, detail: Any = None) -> None:
                _child_send(
                    conn,
                    (
                        "status",
                        str(stage),
                        int(pid or 0),
                        symbol,
                        detail,
                    ),
                )

            symbol_trades = analysis_runner._backtest_symbol(
                history,
                start,
                end,
                btc_history,
                {},
                progress_callback=_analysis_progress,
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
        state: dict[str, Any] | None = None,
    ) -> tuple[list[SimulatedTrade], dict[str, int]]:
        """Run one symbol in an isolated subprocess with hard child timeouts.

        There are two independent failure classes:
        1. CHILD_BOOT_TIMEOUT_SECONDS covers spawn/     "tested=%d "
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
        state: dict[str, Any] | None = None,
    ) -> tuple[list[SimulatedTrade], dict[str, int]]:
        """Run one symbol in an isolated subprocess with hard child timeouts.

        There are two independent failure classes:
        1. CHILD_BOOT_TIMEOUT_SECONDS covers spawn/
