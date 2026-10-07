from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..analysis.engine import analyze_candles
from ..automation.mexc_client import MexcAPIError
from ..automation.mexc_client import MexcClient
from ..automation.signal_validator import validate_signal
from ..automation.universe import MexcUniverse
from ..config import Settings
from .report import BacktestSummary, summarize
from .simulator import SimulatedTrade, simulate_trade

LOGGER = logging.getLogger(__name__)


# ============================================================
# TIME CONSTANTS
# ============================================================

ONE_HOUR_MS = 3_600_000
FOUR_HOURS_MS = 4 * ONE_HOUR_MS
TWELVE_HOURS_MS = 12 * ONE_HOUR_MS
ONE_DAY_MS = 24 * ONE_HOUR_MS

MAX_HOLD_MINUTES = 72 * 60

# Historical warmups
WARMUP_1D = 300 * ONE_DAY_MS
WARMUP_4H = 45 * ONE_DAY_MS
WARMUP_1H = 20 * ONE_DAY_MS

MAX_BACKTEST_SYMBOLS = 200


# ============================================================
# EXCEPTIONS
# ============================================================

class BacktestAnalysisTimeout(TimeoutError):
    pass


class BacktestAnalysisProcessError(RuntimeError):
    pass


class BacktestAlreadyRunning(RuntimeError):
    pass


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass(frozen=True)
class SymbolHistory:
    symbol: str
    candles_1d: list
    candles_12h: list
    candles_4h: list
    candles_1h: list

    # Cached timestamp arrays.
    times_1d: tuple[int, ...] = ()
    times_12h: tuple[int, ...] = ()
    times_4h: tuple[int, ...] = ()
    times_1h: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.times_1d:
            object.__setattr__(
                self,
                "times_1d",
                tuple(_candle_time(x) for x in self.candles_1d),
            )

        if not self.times_12h:
            object.__setattr__(
                self,
                "times_12h",
                tuple(_candle_time(x) for x in self.candles_12h),
            )

        if not self.times_4h:
            object.__setattr__(
                self,
                "times_4h",
                tuple(_candle_time(x) for x in self.candles_4h),
            )

        if not self.times_1h:
            object.__setattr__(
                self,
                "times_1h",
                tuple(_candle_time(x) for x in self.candles_1h),
            )


# ============================================================
# HELPERS
# ============================================================

def _candle_time(row: Any) -> int:
    return int(float(row[0]))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _aggregate_12h_group(group: list) -> list | None:
    """
    Build one deterministic 12H candle from exactly three completed 4H candles.

    Expected MEXC candle layout:
        [timestamp, open, high, low, close, volume, ...]
    """

    if len(group) != 3:
        return None

    timestamps = [_candle_time(row) for row in group]

    # Require perfectly contiguous 4H candles.
    if timestamps[1] != timestamps[0] + FOUR_HOURS_MS:
        return None

    if timestamps[2] != timestamps[1] + FOUR_HOURS_MS:
        return None

    start_ms = timestamps[0]

    # 12H candles must begin at UTC 00:00 or 12:00.
    utc_hour = datetime.fromtimestamp(
        start_ms / 1000,
        tz=timezone.utc,
    ).hour

    if utc_hour not in (0, 12):
        return None

    first = group[0]
    last = group[-1]

    if len(first) < 5 or len(last) < 5:
        return None

    opens = _safe_float(first[1])
    highs = max(_safe_float(row[2]) for row in group)
    lows = min(_safe_float(row[3]) for row in group)
    closes = _safe_float(last[4])

    volume = 0.0
    amount = 0.0

    if len(first) >= 6:
        volume = sum(_safe_float(row[5]) for row in group)

    if len(first) >= 7:
        amount = sum(_safe_float(row[6]) for row in group)

    # Preserve the common MEXC 7-field candle layout.
    if len(first) >= 7:
        return [
            start_ms,
            opens,
            highs,
            lows,
            closes,
            volume,
            amount,
        ]

    return [
        start_ms,
        opens,
        highs,
        lows,
        closes,
        volume,
    ]


def build_12h_candles(candles_4h: list) -> list:
    """
    Deterministically construct 12H candles from completed 4H candles.

    Only complete 3-candle groups are accepted.
    Gaps are rejected rather than silently stitched together.
    """

    if not candles_4h:
        return []

    rows = sorted(
        candles_4h,
        key=_candle_time,
    )

    output: list = []

    current_group: list = []
    current_start: int | None = None

    for row in rows:
        ts = _candle_time(row)

        utc_hour = datetime.fromtimestamp(
            ts / 1000,
            tz=timezone.utc,
        ).hour

        # Start a new 12H block at 00:00 or 12:00 UTC.
        if utc_hour in (0, 12):
            if current_group:
                candle = _aggregate_12h_group(current_group)
                if candle is not None:
                    output.append(candle)

            current_group = [row]
            current_start = ts
            continue

        if not current_group:
            continue

        expected = current_start + len(current_group) * FOUR_HOURS_MS

        if ts != expected or len(current_group) >= 3:
            # Broken/incomplete group.
            current_group = []
            current_start = None
            continue

        current_group.append(row)

        if len(current_group) == 3:
            candle = _aggregate_12h_group(current_group)

            if candle is not None:
                output.append(candle)

            current_group = []
            current_start = None

    return output


def _closed_candles(
    rows: list,
    times: tuple[int, ...],
    decision_close_ms: int,
    candle_duration_ms: int,
) -> list:
    """
    Return only candles whose COMPLETE candle close is <= decision_close_ms.
    """

    cutoff_open_ms = decision_close_ms - candle_duration_ms

    end_index = bisect_right(
        times,
        cutoff_open_ms,
    )

    if end_index <= 0:
        return []

    return rows[:end_index]


def _future_candles(
    rows: list,
    times: tuple[int, ...],
    signal_close_ms: int,
) -> list:
    """
    Return candles beginning at or after the signal close.
    """

    start_index = bisect_left(
        times,
        signal_close_ms,
    )

    return rows[start_index:]


# ============================================================
# BACKTEST RUNNER
# ============================================================

class BacktestRunner:
    """
    Causal paper backtester for:

        1D
         ↓
        12H
         ↓
        4H
         ↓
        1H

    No 5M / 15M / 30M data is used.
    """

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

        self.max_concurrency = max(
            1,
            int(max_concurrency),
        )

        self._running = False
        self._run_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    # ========================================================
    # PERIOD
    # ========================================================

    @staticmethod
    def _period(days: int) -> tuple[int, int]:
        """
        End the historical decision window far enough in the past
        that the maximum holding period has future candles available.
        """

        end_dt = datetime.now(timezone.utc).replace(
            minute=0,
            second=0,
            microsecond=0,
        )

        end_ms = (
            int(end_dt.timestamp() * 1000)
            - MAX_HOLD_MINUTES * 60_000
        )

        start_ms = (
            end_ms
            - int(days) * ONE_DAY_MS
        )

        return start_ms, end_ms

    # ========================================================
    # HISTORY FETCH
    # ========================================================

    async def _fetch_history(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> SymbolHistory:

        async def fetch(
            interval: str,
            left: int,
            right: int,
            limit: int = 2000,
        ) -> list:

            rows = await self.client.get_klines_range(
                symbol,
                interval,
                left,
                right,
                limit=limit,
            )

            return rows or []

        future_end = (
            end_ms
            + MAX_HOLD_MINUTES * 60_000
        )

        # Fetch only the required strategy timeframes.
        candles_1d, candles_4h, candles_1h = await asyncio.gather(
            fetch(
                "Day1",
                start_ms - WARMUP_1D,
                future_end,
            ),
            fetch(
                "Hour4",
                start_ms - WARMUP_4H,
                future_end,
            ),
            fetch(
                "Min60",
                start_ms - WARMUP_1H,
                future_end,
            ),
        )

        # Build 12H ONCE from 4H.
        candles_12h = build_12h_candles(
            candles_4h,
        )

        return SymbolHistory(
            symbol=symbol,
            candles_1d=candles_1d,
            candles_12h=candles_12h,
            candles_4h=candles_4h,
            candles_1h=candles_1h,
        )

    async def _fetch_btc_history(
        self,
        start_ms: int,
        end_ms: int,
    ) -> SymbolHistory:

        async def fetch(
            interval: str,
            left: int,
            right: int,
            limit: int = 2000,
        ) -> list:

            rows = await self.client.get_klines_range(
                "BTC_USDT",
                interval,
                left,
                right,
                limit=limit,
            )

            return rows or []

        future_end = (
            end_ms
            + MAX_HOLD_MINUTES * 60_000
        )

        candles_1d, candles_4h, candles_1h = await asyncio.gather(
            fetch(
                "Day1",
                start_ms - WARMUP_1D,
                future_end,
            ),
            fetch(
                "Hour4",
                start_ms - WARMUP_4H,
                future_end,
            ),
            fetch(
                "Min60",
                start_ms - WARMUP_1H,
                future_end,
            ),
        )

        candles_12h = build_12h_candles(
            candles_4h,
        )

        return SymbolHistory(
            symbol="BTC_USDT",
            candles_1d=candles_1d,
            candles_12h=candles_12h,
            candles_4h=candles_4h,
            candles_1h=candles_1h,
        )

    # ========================================================
    # BTC CONTEXT CACHE
    # ========================================================

    @staticmethod
    def _build_btc_context_cache(
        btc_history: SymbolHistory,
        decision_times: list[int],
    ) -> dict[int, Any]:

        from ..analysis.engine import build_btc_context

        cache: dict[int, Any] = {}

        for signal_close_ms in decision_times:

            btc1d = _closed_candles(
                btc_history.candles_1d,
                btc_history.times_1d,
                signal_close_ms,
                ONE_DAY_MS,
            )

            btc12h = _closed_candles(
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
                or len(btc12h) < 60
                or len(btc4) < 180
                or len(btc1) < 180
            ):
                cache[signal_close_ms] = None
                continue

            try:
                cache[signal_close_ms] = build_btc_context(
                    btc1d,
                    btc12h,
                    btc4,
                    btc1,
                )
            except TypeError:
                # Compatibility fallback if the currently deployed
                # engine still expects the old second argument.
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

        return cache

    # ========================================================
    # SYMBOL SIMULATION
    # ========================================================

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

        trades: list[SimulatedTrade] = []
        diag: dict[str, int] = {}

        rejected = 0
        data_errors = 0
        execution_errors = 0

        seen_keys: set[str] = set()

        active_until_ms = 0

        def inc(
            key: str,
            count: int = 1,
        ) -> None:
            diag[key] = (
                diag.get(key, 0)
                + count
            )

        # ----------------------------------------------------
        # IMPORTANT:
        # Only 1H candles define decision points.
        # ----------------------------------------------------

        for row in history.candles_1h:

            signal_open_ms = _candle_time(row)

            signal_close_ms = (
                signal_open_ms
                + ONE_HOUR_MS
            )

            if signal_close_ms <= start_ms:
                continue

            if signal_close_ms > end_ms:
                continue

            if signal_close_ms <= active_until_ms:
                inc("OVERLAP_SKIPPED")
                continue

            # ------------------------------------------------
            # Causal historical prefixes.
            # No future candles can enter the analysis.
            # ------------------------------------------------

            c1d = _closed_candles(
                history.candles_1d,
                history.times_1d,
                signal_close_ms,
                ONE_DAY_MS,
            )

            c12h = _closed_candles(
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

            # ------------------------------------------------
            # Hard data requirements
            # ------------------------------------------------

            if (
                len(c1d) < 210
                or len(c12h) < 60
                or len(c4) < 180
                or len(c1) < 180
            ):
                data_errors += 1
                inc("ENGINE_DATA_ERRORS")
                inc("ENGINE_DATA_QUALITY_ERROR")

                LOGGER.warning(
                    "BACKTEST DATA_QUALITY_ERROR | "
                    "symbol=%s signal_close_ms=%s "
                    "1d=%d/210 12h=%d/60 4h=%d/180 1h=%d/180",
                    history.symbol,
                    signal_close_ms,
                    len(c1d),
                    len(c12h),
                    len(c4),
                    len(c1),
                )

                # This condition cannot improve during the early
                # part of the test if the required warmup itself
                # is missing.
                break

            try:

                btc_context = (
                    btc_context_cache.get(signal_close_ms)
                    if btc_context_cache is not None
                    else None
                )

                analysis = analyze_candles(
                    history.symbol,
                    c1d,
                    c12h,
                    c4,
                    c1,
                    now_ms=signal_close_ms,
                    btc_context=btc_context,
                )

            except ValueError as exc:

                data_errors += 1

                reason = (
                    str(exc).strip()
                    or "ValueError"
                )

                normalized = (
                    reason.upper()
                    .replace(" ", "_")
                    .replace(":", "")
                    .replace("/", "_")
                )[:120]

                inc("ENGINE_DATA_ERRORS")
                inc("ENGINE_DATA_QUALITY_ERROR")
                inc(
                    f"ENGINE_DATA_QUALITY_{normalized}"
                )

                LOGGER.warning(
                    "BACKTEST DATA_QUALITY_ERROR | "
                    "symbol=%s signal_close_ms=%s reason=%s",
                    history.symbol,
                    signal_close_ms,
                    reason,
                )

                break

            except Exception as exc:

                data_errors += 1

                inc("ENGINE_DATA_ERRORS")
                inc(
                    f"ENGINE_ERROR_{type(exc).__name__}"
                )

                LOGGER.exception(
                    "BACKTEST ENGINE_ERROR | "
                    "symbol=%s signal_close_ms=%s "
                    "type=%s reason=%s",
                    history.symbol,
                    signal_close_ms,
                    type(exc).__name__,
                    exc,
                )

                continue

            # ------------------------------------------------
            # Analysis diagnostics
            # ------------------------------------------------

            inc("CANDLES_EVALUATED")

            if analysis.get("btc_filter_ok") is False:
                inc("BTC_WOULD_BLOCK")

            if not analysis.get("technical_candidate"):

                rejected += 1

                failures = (
                    analysis.get(
                        "technical_gate_failures"
                    )
                    or ["technical_candidate"]
                )

                for failure in failures[:5]:
                    inc(
                        "REJECT_"
                        + str(failure)
                        .upper()
                        .replace(" ", "_")
                    )

                ordered_fails = [
                    (
                 
