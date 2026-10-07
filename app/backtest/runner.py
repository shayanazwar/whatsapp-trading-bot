from __future__ import annotations

"""Causal MEXC Futures backtest runner for the V10 Breakout + Retest swing engine.

Authoritative analysis timeframes:
    1D -> 12H -> 4H -> 1H

This runner deliberately does not fetch, build, analyze, or simulate with:
    any lower timeframe beyond the four authoritative frames.

The runner is designed to match app/analysis/engine.py / gold-v10-breakout-retest-architecture:
- 12H is synthesized once from completed 4H candles using the engine helper.
- MEXC timestamps are canonicalized to milliseconds at ingestion.
- 1H candle closes are the only decision points.
- Historical prefixes are causal: no candle that was not closed at the
  decision timestamp is passed to the engine.
- BTC context uses the engine's 1D/12H/4H/1H API and is observational; the
  engine itself decides whether BTC would block a side.
- Backtest execution is simulated on 1H candles because the authoritative
  strategy no longer uses any lower-timeframe execution data.
- Technical rejection accounting records exactly one FIRST_FAILURE stage while
  retaining the complete list of technical gate failures for forensic analysis.
"""

import asyncio
import inspect
import logging
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ..analysis.engine import (
    build_btc_context,
    convert_candles,
    analyze_candles,
    synthesize_12h_from_4h,
)
from ..automation.mexc_client import MexcAPIError, MexcClient
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


# ============================================================
# TIME CONSTANTS
# ============================================================

ONE_HOUR_MS = 3_600_000
FOUR_HOURS_MS = 4 * ONE_HOUR_MS
TWELVE_HOURS_MS = 12 * ONE_HOUR_MS
ONE_DAY_MS = 24 * ONE_HOUR_MS

MAX_HOLD_MINUTES = 72 * 60
MAX_BACKTEST_SYMBOLS = 200
MAX_KLINE_POINTS = 2000

# Historical warmups required by ENGINE_FIXED.py.
WARMUP_1D = 300 * ONE_DAY_MS
WARMUP_4H = 45 * ONE_DAY_MS
WARMUP_1H = 20 * ONE_DAY_MS

FETCH_TIMEOUT_SECONDS = 180
HEARTBEAT_INTERVAL_SECONDS = 30

SUPPORTED_BACKTEST_DAYS = {1, 7, 30, 60, 90}


# ============================================================
# EXCEPTIONS
# ============================================================

class BacktestAnalysisTimeout(TimeoutError):
    """Raised when one symbol exceeds the analysis watchdog."""


class BacktestAnalysisProcessError(RuntimeError):
    """Compatibility exception retained for callers using the old runner."""


class BacktestAlreadyRunning(RuntimeError):
    """Raised when a second backtest is started while one is active."""


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass(frozen=True)
class SymbolHistory:
    """Normalized, sorted, deduplicated candle history for one symbol."""

    symbol: str
    candles_1d: list
    candles_12h: list
    candles_4h: list
    candles_1h: list

    times_1d: tuple[int, ...] = ()
    times_12h: tuple[int, ...] = ()
    times_4h: tuple[int, ...] = ()
    times_1h: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.times_1d:
            object.__setattr__(
                self,
                "times_1d",
                tuple(_candle_time(c) for c in self.candles_1d),
            )
        if not self.times_12h:
            object.__setattr__(
                self,
                "times_12h",
                tuple(_candle_time(c) for c in self.candles_12h),
            )
        if not self.times_4h:
            object.__setattr__(
                self,
                "times_4h",
                tuple(_candle_time(c) for c in self.candles_4h),
            )
        if not self.times_1h:
            object.__setattr__(
                self,
                "times_1h",
                tuple(_candle_time(c) for c in self.candles_1h),
            )


# ============================================================
# CANDLE / TIME HELPERS
# ============================================================

def _candle_time(row: Any) -> int:
    """Return canonical millisecond candle-open time.

    MEXC Futures responses are normalized to ms by MexcClient, but this helper
    also accepts raw second timestamps so the Runner cannot silently suffer a
    1000x unit mismatch if an alternate client/raw fixture is supplied.
    """

    if isinstance(row, dict):
        raw = row.get(
            "time",
            row.get(
                "timestamp",
                row.get("openTime", row.get("ts")),
            ),
        )
    else:
        raw = row[0]

    ts = int(float(raw))
    if ts < 10**12:
        ts *= 1000
    return ts


def _canonicalize(raw_rows: Iterable[Any] | None) -> list:
    """Normalize, validate, sort and deduplicate candle rows exactly once."""

    return list(convert_candles(raw_rows or []))


def _normalize_interval(interval: str) -> tuple[int, str]:
    """Return interval duration and MEXC API interval string."""

    mapping = {
        "Day1": ONE_DAY_MS,
        "Hour4": FOUR_HOURS_MS,
        "Min60": ONE_HOUR_MS,
    }
    if interval not in mapping:
        raise ValueError(
            f"Unsupported backtest interval {interval!r}; "
            "allowed: Day1, Hour4, Min60"
        )
    return mapping[interval], interval


def _closed_candles(
    rows: list,
    times: tuple[int, ...],
    decision_close_ms: int,
    candle_duration_ms: int,
) -> list:
    """Return only candles whose complete close is <= decision timestamp."""

    cutoff_open_ms = int(decision_close_ms) - int(candle_duration_ms)
    end_index = bisect_right(times, cutoff_open_ms)
    return rows[:end_index] if end_index > 0 else []


def _future_candles(
    rows: list,
    times: tuple[int, ...],
    signal_close_ms: int,
) -> list:
    """Return future candles beginning at the signal close/open boundary."""

    start_index = bisect_left(times, int(signal_close_ms))
    return rows[start_index:]


def _utc_hour(timestamp_ms: int) -> int:
    return datetime.fromtimestamp(
        int(timestamp_ms) / 1000,
        tz=timezone.utc,
    ).hour


def _period(days: int) -> tuple[int, int]:
    """Calculate a causal historical window with a 72h future tail."""

    end_dt = datetime.now(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    end_ms = int(end_dt.timestamp() * 1000) - MAX_HOLD_MINUTES * 60_000
    start_ms = end_ms - int(days) * ONE_DAY_MS
    return start_ms, end_ms


# ============================================================
# 12H / FETCH HELPERS
# ============================================================

async def _fetch_paged_range(
    client: MexcClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int = MAX_KLINE_POINTS,
) -> list:
    """Fetch a bounded range in <=2000-candle pages, then normalize once.

    Page boundaries advance by the source interval, so the same open timestamp
    is never requested twice by construction. Returned candles are then merged
    and canonicalized once through the authoritative engine converter.
    """

    interval_ms, mexc_interval = _normalize_interval(interval)
    left = int(start_ms)
    right = int(end_ms)
    if right < left:
        return []

    page_limit = max(10, min(int(limit), MAX_KLINE_POINTS))
    all_rows: list[Any] = []

    # 2000 opens fit in (limit - 1) intervals when both ends are inclusive.
    page_span = interval_ms * (page_limit - 1)
    cursor = left
    pages = 0

    while cursor <= right:
        page_end = min(right, cursor + page_span)
        rows = await client.get_klines_range(
            symbol,
            mexc_interval,
            cursor,
            page_end,
            limit=page_limit,
        )
        rows = rows or []
        pages += 1

        if rows:
            all_rows.extend(rows)

            normalized_page = _canonicalize(rows)
            if normalized_page:
                last_ts = _candle_time(normalized_page[-1])
                next_cursor = last_ts + interval_ms
                if next_cursor > cursor:
                    cursor = next_cursor
                else:
                    cursor = page_end + interval_ms
            else:
                cursor = page_end + interval_ms
        else:
            cursor = page_end + interval_ms

        if pages > 100:
            raise MexcAPIError(
                f"Historical kline paging exceeded 100 pages for {symbol} {interval}"
            )

        # A server response shorter than the requested page may be caused by
        # a data gap. The next cursor remains based on the last returned bar,
        # allowing later valid history to be recovered.

    return _canonicalize(all_rows)


async def _fetch_symbol_history(
    client: MexcClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> SymbolHistory:
    """Fetch exactly the authoritative 1D/12H/4H/1H source set."""

    future_end = int(end_ms) + MAX_HOLD_MINUTES * 60_000

    c1d_raw, c4_raw, c1_raw = await asyncio.gather(
        _fetch_paged_range(
            client,
            symbol,
            "Day1",
            start_ms - WARMUP_1D,
            future_end,
        ),
        _fetch_paged_range(
            client,
            symbol,
            "Hour4",
            start_ms - WARMUP_4H,
            future_end,
        ),
        _fetch_paged_range(
            client,
            symbol,
            "Min60",
            start_ms - WARMUP_1H,
            future_end,
        ),
    )

    # 12H is built ONCE by the same helper used by ENGINE_FIXED.py.
    c12 = list(synthesize_12h_from_4h(c4_raw))

    return SymbolHistory(
        symbol=str(symbol).upper(),
        candles_1d=c1d_raw,
        candles_12h=c12,
        candles_4h=c4_raw,
        candles_1h=c1_raw,
    )


# ============================================================
# 1H-BASED PAPER EXECUTION
# ============================================================

def _safe_number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if result == result and result not in (float("inf"), -float("inf")) else default


def simulate_trade_1h(
    signal: dict[str, Any],
    future_candles: Iterable[Any],
    *,
    signal_close_time_ms: int,
    fee_rate: float = DEFAULT_FEE_RATE,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    max_holding_minutes: float | None = None,
) -> SimulatedTrade | None:
    """Single authoritative 1H paper simulator used by the V9.2 Runner."""
    return simulate_trade(
        signal,
        future_candles,
        signal_close_time_ms=int(signal_close_time_ms),
        fee_rate=float(fee_rate),
        slippage_bps=float(slippage_bps),
        max_holding_minutes=max_holding_minutes,
    )


# ============================================================
# BACKTEST RUNNER
# ============================================================

class BacktestRunner:
    """Causal paper backtester for the V9.2 1D/12H/4H/1H engine."""

    def __init__(
        self,
        client: MexcClient,
        universe: MexcUniverse,
        settings: Settings,
        max_concurrency: int = 3,
    ) -> None:
        self.client = client
        self.universe = universe
        self.settings = settings
        self.max_concurrency = max(1, int(max_concurrency))
        self._running = False
        self._run_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @staticmethod
    def _period(days: int) -> tuple[int, int]:
        return _period(days)

    async def _fetch_history(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> SymbolHistory:
        return await _fetch_symbol_history(
            self.client,
            symbol,
            start_ms,
            end_ms,
        )

    async def _fetch_btc_history(
        self,
        start_ms: int,
        end_ms: int,
    ) -> SymbolHistory:
        return await _fetch_symbol_history(
            self.client,
            "BTC_USDT",
            start_ms,
            end_ms,
        )

    @staticmethod
    def _build_btc_context_cache(
        btc_history: SymbolHistory,
        decision_times: list[int],
    ) -> dict[int, Any]:
        """Build causal BTC context once per distinct 1H decision time."""

        cache: dict[int, Any] = {}
        ordered_times = sorted(set(int(x) for x in decision_times))

        for signal_close_ms in ordered_times:
            btc1d = _closed_candles(
                btc_history.candles_1d,
                btc_history.times_1d,
                signal_close_ms,
                ONE_DAY_MS,
            )
            btc12 = _closed_candles(
                btc_history.candles_12h,
                btc_history.times_12h,
                signal_close_ms,
                TWELVE_HOURS_MS,
            )
            btc4 = _closed_candles(
                btc_history.candles_4h,
                btc_history.times_4h,
                signal_close_ms,
                FOUR_HOURS_MS,
            )
            btc1 = _closed_candles(
                btc_history.candles_1h,
                btc_history.times_1h,
                signal_close_ms,
                ONE_HOUR_MS,
            )

            if (
                len(btc1d) < 210
                or len(btc12) < 60
                or len(btc4) < 180
                or len(btc1) < 180
            ):
                cache[signal_close_ms] = None
                continue

            try:
                cache[signal_close_ms] = build_btc_context(
                    btc1d,
                    btc12,
                    btc4,
                    btc1,
                )
            except TypeError:
                # Compatibility with an older deployed helper. The V9.2
                # engine does not use this branch.
                try:
                    cache[signal_close_ms] = build_btc_context(
                        btc1d,
                        None,
                        btc4,
                        btc1,
                    )
                except Exception:
                    LOGGER.exception(
                        "BACKTEST BTC_CONTEXT_ERROR | signal_close_ms=%s",
                        signal_close_ms,
                    )
                    cache[signal_close_ms] = None
            except Exception:
                LOGGER.exception(
                    "BACKTEST BTC_CONTEXT_ERROR | signal_close_ms=%s",
                    signal_close_ms,
                )
                cache[signal_close_ms] = None

        return cache

    def _simulate_symbol(
        self,
        history: SymbolHistory,
        start_ms: int,
        end_ms: int,
        btc_context_cache: dict[int, Any] | None = None,
    ) -> tuple[
        list[SimulatedTrade],
        dict[str, int],
        int,
        int,
        int,
    ]:
        """Evaluate one symbol at every causal 1H close in the backtest window."""

        symbol_started = time.monotonic()
        trades: list[SimulatedTrade] = []
        diagnostics: dict[str, int] = {}
        data_errors = 0
        simulation_errors = 0
        engine_errors = 0

        seen_structures: set[tuple[Any, ...]] = set()
        active_until_ms = 0

        def inc(key: str, amount: int = 1) -> None:
            diagnostics[key] = diagnostics.get(key, 0) + int(amount)

        # Only 1H bars create decisions.
        for row in history.candles_1h:
            signal_open_ms = _candle_time(row)
            signal_close_ms = signal_open_ms + ONE_HOUR_MS

            if signal_close_ms <= start_ms:
                continue
            if signal_close_ms > end_ms:
                continue
            if signal_close_ms <= active_until_ms:
                inc("OVERLAP_SKIPPED")
                continue

            c1d = _closed_candles(
                history.candles_1d,
                history.times_1d,
                signal_close_ms,
                ONE_DAY_MS,
            )
            c12 = _closed_candles(
                history.candles_12h,
                history.times_12h,
                signal_close_ms,
                TWELVE_HOURS_MS,
            )
            c4 = _closed_candles(
                history.candles_4h,
                history.times_4h,
                signal_close_ms,
                FOUR_HOURS_MS,
            )
            c1 = _closed_candles(
                history.candles_1h,
                history.times_1h,
                signal_close_ms,
                ONE_HOUR_MS,
            )

            if (
                len(c1d) < 210
                or len(c12) < 60
                or len(c4) < 180
                or len(c1) < 180
            ):
                # This is a warmup condition, not a signal rejection. Avoid
                # exploding the diagnostic/error count for every early hour.
                inc("WARMUP_SKIPPED")
                continue

            btc_context = (
                btc_context_cache.get(signal_close_ms)
                if btc_context_cache is not None
                else None
            )

            try:
                analysis = analyze_candles(
                    history.symbol,
                    c1d,
                    c12,
                    c4,
                    c1,
                    now_ms=signal_close_ms,
                    btc_context=btc_context,
                    estimated_round_trip_cost_pct=float(getattr(self.settings, "estimated_round_trip_cost_pct", 0.0015)),
                )
                inc("ENGINE_CALLS")
                inc("CANDLES_EVALUATED")
                inc("ENGINE_SUCCESS")
            except ValueError as exc:
                data_errors += 1
                inc("ENGINE_DATA_ERRORS")
                reason = str(exc).strip() or "ValueError"
                normalized = (
                    reason.upper()
                    .replace(" ", "_")
                    .replace(":", "")
                    .replace("/", "_")
                )[:120]
                inc(f"ENGINE_DATA_QUALITY_{normalized}")
                LOGGER.warning(
                    "BACKTEST DATA_QUALITY_ERROR | "
                    "symbol=%s signal_close_ms=%s reason=%s",
                    history.symbol,
                    signal_close_ms,
                    reason,
                )
                # A historical data gap can roll out of the engine's lookback
                # window later. Do not prematurely terminate the entire symbol.
                continue
            except Exception as exc:
                engine_errors += 1
                inc("ENGINE_ERRORS")
                inc(f"ENGINE_ERROR_{type(exc).__name__}")
                LOGGER.exception(
                    "BACKTEST ENGINE_ERROR | "
                    "symbol=%s signal_close_ms=%s reason=%s",
                    history.symbol,
                    signal_close_ms,
                    exc,
                )
                continue

            if analysis.get("btc_filter_ok") is False:
                inc("BTC_WOULD_BLOCK")

            if not analysis.get("technical_candidate"):
                inc("TECHNICAL_REJECT")
                failures = list(analysis.get("technical_gate_failures") or [])
                first_failure = str(
                    analysis.get("first_failure")
                    or analysis.get("rejection_stage")
                    or (failures[0] if failures else "TECHNICAL_CANDIDATE")
                )
                inc(f"FIRST_FAILURE_{first_failure.upper().replace(' ', '_').replace(':', '').replace('/', '_').replace('-', '_')}")
                for failure in failures:
                    key = (
                        "REJECT_"
                        + str(failure)
                        .upper()
                        .replace(" ", "_")
                        .replace(":", "")
                        .replace("/", "_")
                        .replace("-", "_")
                    )
                    inc(key)
                continue

            inc("TECHNICAL_ACCEPT")
            inc("FIRST_FAILURE_NONE")

            side = str(
                analysis.get("setup")
                or analysis.get("setup_candidate")
                or ""
            ).upper()
            if side not in {"LONG", "SHORT"}:
                inc("INVALID_ENGINE_SIDE")
                continue

            inc(f"FULL_ENGINE_ACCEPT_{side}")

            bos_level = (
                analysis.get("long_bos_level")
                if side == "LONG"
                else analysis.get("short_bos_level")
            )
            structure_key = (
                side,
                analysis.get("setup_bos_time"),
                bos_level,
                analysis.get("setup_retest_1h_time") or analysis.get("setup_retest_time"),
            )
            if structure_key in seen_structures:
                inc("DUPLICATE_STRUCTURE_SKIPPED")
                continue
            seen_structures.add(structure_key)

            future = _future_candles(
                history.candles_1h,
                history.times_1h,
                signal_close_ms,
            )
            if not future:
                inc("NO_FUTURE_CANDLES")
                continue

            max_hold = float(
                _safe_number(analysis.get("intraday_max_hold_minutes"))
                or getattr(
                    self.settings,
                    "backtest_max_holding_minutes",
                    MAX_HOLD_MINUTES,
                )
            )
            fee_rate = float(
                _safe_number(
                    getattr(
                        self.settings,
                        "backtest_fee_rate",
                        DEFAULT_FEE_RATE,
                    )
                )
                or 0.0
            )
            # If the setting is missing, use the simulator's documented default.
            if not hasattr(self.settings, "backtest_fee_rate"):
                fee_rate = DEFAULT_FEE_RATE

            slippage_bps = float(
                _safe_number(
                    getattr(
                        self.settings,
                        "backtest_slippage_bps",
                        DEFAULT_SLIPPAGE_BPS,
                    )
                )
                or 0.0
            )
            if not hasattr(self.settings, "backtest_slippage_bps"):
                slippage_bps = DEFAULT_SLIPPAGE_BPS

            try:
                trade = simulate_trade_1h(
                    analysis,
                    future,
                    signal_close_time_ms=signal_close_ms,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    max_holding_minutes=max_hold,
                )
            except Exception as exc:
                simulation_errors += 1
                inc("SIMULATION_ERRORS")
                LOGGER.exception(
                    "BACKTEST SIMULATION_ERROR | symbol=%s signal_close_ms=%s reason=%s",
                    history.symbol,
                    signal_close_ms,
                    exc,
                )
                continue

            if trade is None:
                inc("SIMULATION_NO_TRADE")
                if str(analysis.get("entry_mode") or "MARKET").upper() == "LIMIT":
                    inc("SIMULATION_LIMIT_NOT_FILLED")
                continue

            trades.append(trade)
            inc("SIMULATION_ACCEPT")
            inc(f"OUTCOME_{trade.outcome}")

            if trade.tp1_hit:
                inc("TP1_HIT")
            if trade.tp2_hit:
                inc("TP2_HIT")
            if trade.sl_hit:
                inc("SL_HIT")
            if trade.expired:
                inc("EXPIRY")

            if trade.exit_time_ms is not None:
                active_until_ms = max(
                    active_until_ms,
                    int(trade.exit_time_ms),
                )

        diagnostics["CANDIDATES_ENGINE_EVALUATED"] = diagnostics.get(
            "ENGINE_CALLS",
            0,
        )
        diagnostics["ENGINE_OUTCOME_UNKNOWN_CALLS"] = 0
        diagnostics["TECHNICAL_ACCOUNTING_GAP"] = max(
            0,
            diagnostics.get("ENGINE_SUCCESS", 0)
            - diagnostics.get("TECHNICAL_ACCEPT", 0)
            - diagnostics.get("TECHNICAL_REJECT", 0),
        )
        diagnostics["ANALYSIS_TOTAL_TIME_MS"] = int(
            (time.monotonic() - symbol_started) * 1000
        )

        LOGGER.info(
            "BACKTEST SYMBOL COMPLETE | "
            "symbol=%s engine_calls=%d technical_accept=%d technical_reject=%d "
            "trades=%d data_errors=%d engine_errors=%d simulation_errors=%d "
            "seconds=%.2f",
            history.symbol,
            diagnostics.get("ENGINE_CALLS", 0),
            diagnostics.get("TECHNICAL_ACCEPT", 0),
            diagnostics.get("TECHNICAL_REJECT", 0),
            len(trades),
            data_errors,
            engine_errors,
            simulation_errors,
            time.monotonic() - symbol_started,
        )

        return (
            trades,
            diagnostics,
            data_errors,
            simulation_errors,
            engine_errors,
        )

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
                pass

            elapsed = time.monotonic() - started
            processed = int(state.get("processed", 0))
            total = int(state.get("total", 0))
            LOGGER.info(
                "BACKTEST HEARTBEAT | days=%d phase=%s processed=%d/%d "
                "tested=%d signals=%d data_errors=%d engine_errors=%d "
                "simulation_errors=%d analysis_errors=%d elapsed=%.1fs symbol=%s",
                days,
                state.get("phase", "UNKNOWN"),
                processed,
                total,
                int(state.get("tested", 0)),
                int(state.get("signals", 0)),
                int(state.get("data_errors", 0)),
                int(state.get("engine_errors", 0)),
                int(state.get("simulation_errors", 0)),
                int(state.get("analysis_errors", 0)),
                elapsed,
                state.get("symbol", "-"),
            )

    async def run(self, days: int) -> BacktestSummary:
        """Run a complete 1D/7D/30D/60D/90D causal paper backtest."""

        days = int(days)
        if days not in SUPPORTED_BACKTEST_DAYS:
            raise ValueError(
                "Supported backtests: 1D, 7D, 30D, 60D, 90D"
            )

        if self._running or self._run_lock.locked():
            raise BacktestAlreadyRunning(
                "A backtest is already running. Please wait for it to finish."
            )

        async with self._run_lock:
            self._running = True
            started = time.monotonic()
            stop_event = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._heartbeat(
                    state := {
                        "phase": "INITIALIZING",
                        "total": 0,
                        "processed": 0,
                        "tested": 0,
                        "data_errors": 0,
                        "engine_errors": 0,
                        "simulation_errors": 0,
                        "analysis_errors": 0,
                        "signals": 0,
                        "symbol": "-",
                    },
                    started,
                    stop_event,
                    days,
                ),
                name="backtest-heartbeat",
            )

            diagnostics: dict[str, int] = {}
            trades: list[SimulatedTrade] = []

            def inc_global(key: str, amount: int = 1) -> None:
                diagnostics[key] = diagnostics.get(key, 0) + int(amount)

            try:
                start_ms, end_ms = self._period(days)
                state["phase"] = "UNIVERSE"

                all_symbols = list(
                    await asyncio.wait_for(
                        self.universe.refresh(),
                        timeout=FETCH_TIMEOUT_SECONDS,
                    )
                )
                max_symbols = max(1, min(
                    int(getattr(self.settings, "backtest_max_symbols", MAX_BACKTEST_SYMBOLS)),
                    MAX_BACKTEST_SYMBOLS,
                ))
                symbols = []
                seen_symbols: set[str] = set()
                for raw_symbol in all_symbols:
                    symbol = str(raw_symbol).strip().upper()
                    if not symbol or symbol in seen_symbols:
                        continue
                    seen_symbols.add(symbol)
                    symbols.append(symbol)
                    if len(symbols) >= max_symbols:
                        break

                if not symbols:
                    LOGGER.warning("BACKTEST EMPTY UNIVERSE | days=%d", days)
                    summary_payload = {
                        "days": days,
                        "period_start_ms": int(start_ms),
                        "period_end_ms": int(end_ms),
                        "coins_selected": 0,
                        "coins_tested": 0,
                        "data_errors": 0,
                        "execution_errors": 0,
                        "rejected_setups": 0,
                        "trades": [],
                        "diagnostics": {"NO_ELIGIBLE_SYMBOLS": 1, "CURRENT_UNIVERSE_SNAPSHOT_BIAS": 1},
                    }
                    params = inspect.signature(summarize).parameters
                    if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                        summary_payload = {k: v for k, v in summary_payload.items() if k in params}
                    return summarize(**summary_payload)

                state["total"] = len(symbols)
                inc_global("SYMBOLS_DISCOVERED", len(all_symbols))
                inc_global("SYMBOLS_SELECTED", len(symbols))

                LOGGER.info(
                    "BACKTEST START | days=%d symbols=%d start=%d end=%d "
                    "strategy=1D>12H>4H>1H",
                    days,
                    len(symbols),
                    start_ms,
                    end_ms,
                )

                # --------------------------------------------------------
                # BTC history/context. Failure is non-fatal because the
                # V9.2 engine treats unavailable BTC context as abstain.
                # --------------------------------------------------------
                state["phase"] = "BTC_DATA"
                btc_history: SymbolHistory | None = None
                try:
                    btc_history = await asyncio.wait_for(
                        self._fetch_btc_history(start_ms, end_ms),
                        timeout=FETCH_TIMEOUT_SECONDS,
                    )
                    inc_global("BTC_DATA_READY")
                except Exception as exc:
                    inc_global("BTC_DATA_ERROR")
                    LOGGER.exception(
                        "BACKTEST BTC DATA ERROR | reason=%s",
                        exc,
                    )

                btc_context_cache: dict[int, Any] = {}
                if btc_history is not None:
                    decision_times = [
                        int(t) + ONE_HOUR_MS
                        for t in btc_history.times_1h
                        if start_ms < int(t) + ONE_HOUR_MS <= end_ms
                    ]
                    state["phase"] = "BTC_CONTEXT"
                    btc_context_cache = await asyncio.to_thread(
                        self._build_btc_context_cache,
                        btc_history,
                        decision_times,
                    )
                    inc_global(
                        "BTC_CONTEXT_CACHE_ITEMS",
                        len(btc_context_cache),
                    )

                # --------------------------------------------------------
                # Symbol workers
                # --------------------------------------------------------
                state["phase"] = "SYMBOL_ANALYSIS"
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

                            state["symbol"] = symbol
                            data_started = time.monotonic()

                            try:
                                history = await asyncio.wait_for(
                                    self._fetch_history(
                                        symbol,
                                        start_ms,
                                        end_ms,
                                    ),
                                    timeout=FETCH_TIMEOUT_SECONDS,
                                )
                                async with state_lock:
                                    inc_global("SYMBOLS_DATA_READY")
                                    state["tested"] += 1
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                async with state_lock:
                                    state["data_errors"] += 1
                                    inc_global("SYMBOLS_DATA_ERROR")
                                    inc_global(
                                        f"DATA_ERROR_{type(exc).__name__}"
                                    )
                                LOGGER.exception(
                                    "BACKTEST DATA ERROR | worker=%d symbol=%s reason=%s",
                                    worker_id,
                                    symbol,
                                    exc,
                                )
                                continue

                            data_seconds = time.monotonic() - data_started
                            inc_global(
                                "DATA_SECONDS_X1000",
                                int(data_seconds * 1000),
                            )

                            try:
                                (
                                    symbol_trades,
                                    symbol_diag,
                                    symbol_data_errors,
                                    symbol_simulation_errors,
                                    symbol_engine_errors,
                                ) = await asyncio.wait_for(
                                    asyncio.to_thread(
                                        self._simulate_symbol,
                                        history,
                                        start_ms,
                                        end_ms,
                                        btc_context_cache,
                                    ),
                                    timeout=max(1.0, float(getattr(self.settings, "backtest_analysis_timeout_seconds", 120.0))),
                                )
                            except asyncio.CancelledError:
                                raise
                            except asyncio.TimeoutError as exc:
                                state["analysis_errors"] = int(state.get("analysis_errors", 0)) + 1
                                inc_global("ANALYSIS_TIMEOUT")
                                LOGGER.error(
                                    "BACKTEST ANALYSIS TIMEOUT | worker=%d symbol=%s reason=%s",
                                    worker_id,
                                    symbol,
                                    exc,
                                )
                                continue
                            except Exception as exc:
                                state["analysis_errors"] = int(state.get("analysis_errors", 0)) + 1
                                inc_global("SYMBOLS_ANALYSIS_FAILED")
                                inc_global(
                                    f"ANALYSIS_ERROR_{type(exc).__name__}"
                                )
                                LOGGER.exception(
                                    "BACKTEST ANALYSIS ERROR | worker=%d symbol=%s reason=%s",
                                    worker_id,
                                    symbol,
                                    exc,
                                )
                                continue

                            async with state_lock:
                                for key, value in symbol_diag.items():
                                    inc_global(key, int(value))
                                state["data_errors"] += int(symbol_data_errors)
                                state["engine_errors"] += int(symbol_engine_errors)
                                state["simulation_errors"] += int(symbol_simulation_errors)
                                trades.extend(symbol_trades)
                                state["signals"] = len(trades)

                            LOGGER.info(
                                "BACKTEST PROGRESS | worker=%d days=%d processed=%d/%d "
                                "tested=%d signals=%d data_errors=%d engine_errors=%d "
                                "simulation_errors=%d symbol=%s",
                                worker_id,
                                days,
                                state["processed"],
                                len(symbols),
                                state["tested"],
                                len(trades),
                                state["data_errors"],
                                state["engine_errors"],
                                state["simulation_errors"],
                                symbol,
                            )

                        finally:
                            if symbol is not None:
                                async with state_lock:
                                    state["processed"] += 1
                                    processed = int(state["processed"])
                                    total = int(state["total"])
                                LOGGER.info(
                                    "BACKTEST SYMBOL WORKER FINISHED | "
                                    "worker=%d symbol=%s processed=%d/%d",
                                    worker_id,
                                    symbol,
                                    processed,
                                    total,
                                )
                            queue.task_done()

                workers = [
                    asyncio.create_task(
                        worker(index),
                        name=f"backtest-worker-{index}",
                    )
                    for index in range(self.max_concurrency)
                ]

                await asyncio.gather(*workers)

                state["phase"] = "FINALIZING"
                trades.sort(key=lambda trade: trade.signal_time_ms)

                # Build the complete current summary payload.  The deployed
                # report.py may be one of two compatible schema generations:
                # older builds do not have the four forensic keyword fields,
                # while newer builds require them.  Detect the active callable
                # signature rather than allowing an avoidable TypeError to abort
                # an otherwise completed backtest.
                rejected_setups = int(
                    diagnostics.get("TECHNICAL_REJECT", 0)
                )
                execution_errors = int(
                    state.get("engine_errors", 0)
                    + state.get("simulation_errors", 0)
                    + state.get("analysis_errors", 0)
                )

                summary_payload = {
                    "days": days,
                    "coins_selected": len(symbols),
                    "coins_tested": int(state["tested"]),
                    "data_errors": int(state["data_errors"]),
                    "period_start_ms": int(start_ms),
                    "period_end_ms": int(end_ms),
                    "execution_errors": execution_errors,
                    "rejected_setups": rejected_setups,
                    "trades": trades,
                    "diagnostics": diagnostics,
                }

                try:
                    summarize_parameters = inspect.signature(summarize).parameters
                    accepts_kwargs = any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in summarize_parameters.values()
                    )
                    if not accepts_kwargs:
                        summary_payload = {
                            key: value
                            for key, value in summary_payload.items()
                            if key in summarize_parameters
                        }

                    summary = summarize(**summary_payload)
                except (TypeError, ValueError) as exc:
                    LOGGER.exception(
                        "BACKTEST SUMMARY ERROR | summarize() schema mismatch | reason=%s",
                        exc,
                    )
                    raise

                LOGGER.info(
                    "BACKTEST COMPLETE | days=%d tested=%d/%d signals=%d "
                    "data_errors=%d engine_errors=%d simulation_errors=%d analysis_errors=%d "
                    "duration=%.2fs",
                    days,
                    state["tested"],
                    len(symbols),
                    len(trades),
                    state["data_errors"],
                    state["engine_errors"],
                    state["simulation_errors"],
                    state.get("analysis_errors", 0),
                    time.monotonic() - started,
                )

                return summary

            finally:
                stop_event.set()
                heartbeat.cancel()
                await asyncio.gather(
                    heartbeat,
                    return_exceptions=True,
                )
                self._running = False


# ============================================================
# OPTIONAL MODULE-LEVEL HELPERS
# ============================================================

def build_12h_candles(candles_4h: Iterable[Any] | None) -> list:
    """Compatibility wrapper around ENGINE_FIXED.py's 12H constructor."""

    normalized = _canonicalize(candles_4h or [])
    return list(synthesize_12h_from_4h(normalized))


__all__ = [
    "BacktestAlreadyRunning",
    "BacktestAnalysisTimeout",
    "BacktestAnalysisProcessError",
    "BacktestRunner",
    "SymbolHistory",
    "build_12h_candles",
    "simulate_trade_1h",
]
