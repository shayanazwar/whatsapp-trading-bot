from __future__ import annotations

"""Causal MEXC Futures backtest runner for the V11 swing engine.

Authoritative analysis timeframes:
    1D -> 12H -> 4H -> 1H

This runner deliberately does not fetch, build, analyze, or simulate with:
    any lower timeframe beyond the four authoritative frames.

The runner is designed to match V11 value-pullback engine:
- 12H is synthesized once from completed 4H candles using the engine helper.
- MEXC timestamps are canonicalized to milliseconds at ingestion.
- 1H candle closes are the only decision points.
- Historical prefixes are causal: no candle that was not closed at the
  decision timestamp is passed to the engine.
- BTC context uses the engine's 1D/12H/4H/1H API and is observational; the
  engine itself decides whether BTC would block a side.
- Backtest execution is simulated on 1H candles because the authoritative
  strategy no longer uses any lower-timeframe execution data.
"""

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
from pathlib import Path
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ..analysis.engine import (
    V11_MAX_TRIGGER_BARS,
    V11_MIN_IMPULSE_ATR,
    V11_SHORT_RANGE_RELAXED,
    V11_SL_MODE,
    analyze_candles,
    build_btc_context,
    convert_candles,
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

SUPPORTED_BACKTEST_DAYS = {1, 7, 30, 60, 90, 180, 365}

# Backtest reproducibility is an infrastructure concern, not a strategy rule.
# A snapshot records the exact universe selection and SHA-256 fingerprints of
# the historical candle sets used for that period. TP mode is deliberately NOT
# part of the snapshot identity so CONTROL/1.5R/2.0R/2.5R reuse one dataset.
BACKTEST_SNAPSHOT_SCHEMA = 1
BACKTEST_SNAPSHOT_DIR_ENV = "V11_BACKTEST_SNAPSHOT_DIR"



# ============================================================
# EXCEPTIONS
# ============================================================

class BacktestAnalysisTimeout(TimeoutError):
    """Raised when one symbol exceeds the analysis watchdog."""


class BacktestAnalysisProcessError(RuntimeError):
    """Compatibility exception retained for callers using the old runner."""


class BacktestAlreadyRunning(RuntimeError):
    """Raised when a second backtest is started while one is active."""


class BacktestSnapshotError(RuntimeError):
    """Raised when a frozen backtest dataset cannot be trusted or reused."""


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


@dataclass(frozen=True)
class BacktestTPConfig:
    """Immutable TP selection scoped to one backtest execution."""

    mode: str = "CONTROL"
    r_multiple: float | None = None

    @classmethod
    def from_mode(cls, mode: str) -> "BacktestTPConfig":
        normalized = str(mode or "CONTROL").strip().upper()
        mapping = {
            "CONTROL": ("CONTROL", None),
            "1.5R": ("1.5R", 1.5),
            "2R": ("2.0R", 2.0),
            "2.0R": ("2.0R", 2.0),
            "2.5R": ("2.5R", 2.5),
        }
        selected = mapping.get(normalized)
        if selected is None:
            raise ValueError(
                "Invalid TP. Use CONTROL, 1.5R, 2.0R, or 2.5R."
            )
        return cls(mode=selected[0], r_multiple=selected[1])


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
    """Return an exact, hour-aligned backtest period with no future candles.

    Fixed periods accept Unix seconds or milliseconds for compatibility, but
    the requested duration must exactly match the BACKTEST command. A fixed
    end timestamp may not be in the future or inside the current 1H candle.
    This prevents right-censoring and moving-window drift from contaminating A/B tests.
    """
    days = int(days)
    raw_start = os.getenv("V11_BACKTEST_START_MS")
    raw_end = os.getenv("V11_BACKTEST_END_MS")

    def _parse_timestamp(raw: str, name: str) -> int:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"{name} must be an integer Unix timestamp in seconds or milliseconds"
            ) from exc
        return value * 1000 if abs(value) < 100_000_000_000 else value

    if raw_start or raw_end:
        if not raw_end:
            raise ValueError("V11_BACKTEST_END_MS is required when freezing a backtest period")
        end_ms = _parse_timestamp(raw_end, "V11_BACKTEST_END_MS")
        start_ms = _parse_timestamp(raw_start, "V11_BACKTEST_START_MS") if raw_start else end_ms - days * ONE_DAY_MS
        if start_ms >= end_ms:
            raise ValueError("V11_BACKTEST_START_MS must be earlier than V11_BACKTEST_END_MS")
        if end_ms % ONE_HOUR_MS != 0 or start_ms % ONE_HOUR_MS != 0:
            raise ValueError("Fixed backtest start/end must be aligned to whole 1H boundaries")
        if end_ms - start_ms != days * ONE_DAY_MS:
            raise ValueError(f"Fixed backtest period must be exactly {days}D")
        current_hour_ms = int(
            datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).timestamp() * 1000
        )
        if end_ms > current_hour_ms:
            raise ValueError(
                "Fixed backtest end is in the future or inside the current 1H candle; "
                "use a fully completed historical end timestamp"
            )
        return start_ms, end_ms

    end_dt = datetime.now(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    end_ms = int(end_dt.timestamp() * 1000) - MAX_HOLD_MINUTES * 60_000
    start_ms = end_ms - days * ONE_DAY_MS
    return start_ms, end_ms


# ============================================================
# BACKTEST SNAPSHOT / REPRODUCIBILITY HELPERS
# ============================================================

def _snapshot_root(settings: Settings) -> Path:
    raw = str(os.getenv(BACKTEST_SNAPSHOT_DIR_ENV, "")).strip()
    if raw:
        return Path(raw).expanduser()
    db_path = str(getattr(settings, "database_path", "signals.db") or "signals.db")
    try:
        return Path(db_path).expanduser().resolve().parent / "backtest_snapshots"
    except Exception:
        return Path("backtest_snapshots")


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(payload: Any) -> str:
    return _sha256_bytes(_json_bytes(payload))


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(_json_bytes(payload))
    os.replace(tmp, path)


def _strategy_fingerprint(settings: Settings) -> str:
    """Fingerprint signal-generation code/config, excluding TP mode by design."""
    try:
        engine_path = Path(inspect.getfile(analyze_candles))
        engine_hash = _sha256_bytes(engine_path.read_bytes())
    except Exception as exc:
        raise BacktestSnapshotError(
            f"Unable to fingerprint the V11 engine source: {exc}"
        ) from exc

    config = {
        "engine_sha256": engine_hash,
        "engine_version": "V11-1D-12H-4H-1H",
        "V11_MIN_IMPULSE_ATR": float(V11_MIN_IMPULSE_ATR),
        "V11_SL_MODE": str(V11_SL_MODE),
        "V11_MAX_TRIGGER_BARS": int(V11_MAX_TRIGGER_BARS),
        "V11_SHORT_RANGE_RELAXED": bool(V11_SHORT_RANGE_RELAXED),
        "estimated_round_trip_cost_pct": float(
            getattr(settings, "estimated_round_trip_cost_pct", 0.0015)
        ),
    }
    return _sha256_json(config)[:20]


def _universe_selection_fingerprint(universe: MexcUniverse) -> str:
    test_symbols = sorted(str(x).upper() for x in getattr(universe, "test_symbols", set()) or set())
    try:
        refresh_path = Path(inspect.getfile(universe.refresh))
        refresh_hash = _sha256_bytes(refresh_path.read_bytes())
    except Exception as exc:
        raise BacktestSnapshotError(
            f"Unable to fingerprint universe-selection source: {exc}"
        ) from exc
    payload = {
        "refresh_sha256": refresh_hash,
        "test_symbols": test_symbols,
        "max_symbols": int(getattr(universe, "max_symbols", MAX_BACKTEST_SYMBOLS)),
    }
    return _sha256_json(payload)[:12]


def _snapshot_id(
    *,
    start_ms: int,
    end_ms: int,
    max_symbols: int,
    strategy_fingerprint: str,
    universe_selection_fingerprint: str,
) -> str:
    return (
        f"v{BACKTEST_SNAPSHOT_SCHEMA}_{strategy_fingerprint}_"
        f"{int(start_ms)}_{int(end_ms)}_{int(max_symbols)}_"
        f"{universe_selection_fingerprint}"
    )


def _canonical_symbols(symbols: Iterable[Any], max_symbols: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        output.append(symbol)
        if len(output) >= int(max_symbols):
            break
    return output


def _serialize_candles(rows: Iterable[Any]) -> list[Any]:
    output: list[Any] = []
    for candle in rows:
        if isinstance(candle, dict):
            output.append(dict(candle))
        else:
            try:
                output.append(list(candle[:6]))
            except Exception:
                output.append(str(candle))
    return output


def _history_payload(history: SymbolHistory) -> dict[str, Any]:
    # 12H is derived deterministically from 4H and is intentionally not stored
    # as an independent source dataset. This keeps one authoritative source set.
    return {
        "schema": BACKTEST_SNAPSHOT_SCHEMA,
        "symbol": history.symbol,
        "candles_1d": _serialize_candles(history.candles_1d),
        "candles_4h": _serialize_candles(history.candles_4h),
        "candles_1h": _serialize_candles(history.candles_1h),
    }


def _history_fingerprint(history: SymbolHistory) -> str:
    return _sha256_json(_history_payload(history))


def _data_snapshot_hash(data_hashes: Mapping[str, str]) -> str:
    ordered = {str(key): str(data_hashes[key]) for key in sorted(data_hashes)}
    return _sha256_json(ordered)


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BacktestSnapshotError(
            f"Unreadable backtest snapshot manifest: {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise BacktestSnapshotError(f"Invalid backtest snapshot manifest: {path}")
    return payload


def _validate_ready_manifest(
    manifest: Mapping[str, Any],
    *,
    snapshot_id: str,
    start_ms: int,
    end_ms: int,
    max_symbols: int,
    strategy_fingerprint: str,
) -> tuple[list[str], dict[str, str], str, str]:
    if int(manifest.get("schema", -1)) != BACKTEST_SNAPSHOT_SCHEMA:
        raise BacktestSnapshotError("Backtest snapshot schema mismatch; rebuild the snapshot")
    if str(manifest.get("status", "")).upper() != "READY":
        raise BacktestSnapshotError("Backtest snapshot is incomplete; rebuild it")
    if str(manifest.get("snapshot_id")) != snapshot_id:
        raise BacktestSnapshotError("Backtest snapshot identity mismatch")
    if int(manifest.get("period_start_ms", -1)) != int(start_ms) or int(manifest.get("period_end_ms", -1)) != int(end_ms):
        raise BacktestSnapshotError("Backtest snapshot period mismatch")
    if int(manifest.get("max_symbols", -1)) != int(max_symbols):
        raise BacktestSnapshotError("Backtest snapshot universe-size mismatch")
    if str(manifest.get("strategy_fingerprint")) != strategy_fingerprint:
        raise BacktestSnapshotError("Backtest strategy/config fingerprint changed; do not reuse the old snapshot")
    symbols = [str(x).upper() for x in (manifest.get("symbols") or [])]
    expected_hashes = manifest.get("data_hashes") or {}
    if not isinstance(expected_hashes, dict) or not symbols:
        raise BacktestSnapshotError("Backtest snapshot has no frozen symbol/data manifest")
    if len(symbols) != len(set(symbols)):
        raise BacktestSnapshotError("Backtest snapshot contains duplicate symbols")
    if set(expected_hashes) != set(symbols) | {"BTC_USDT"}:
        raise BacktestSnapshotError("Backtest snapshot data manifest does not match its symbol set")
    universe_hash = str(manifest.get("universe_hash") or "")
    data_hash = str(manifest.get("data_snapshot_hash") or "")
    if not universe_hash or not data_hash:
        raise BacktestSnapshotError("Backtest snapshot is missing integrity hashes")
    if _sha256_json(symbols) != universe_hash:
        raise BacktestSnapshotError("Backtest universe hash mismatch")
    if _data_snapshot_hash({str(k): str(v) for k, v in expected_hashes.items()}) != data_hash:
        raise BacktestSnapshotError("Backtest data snapshot hash mismatch")
    return symbols, {str(k): str(v) for k, v in expected_hashes.items()}, universe_hash, data_hash


@dataclass(frozen=True)
class BacktestSnapshot:
    snapshot_id: str
    directory: Path
    symbols: tuple[str, ...]
    expected_data_hashes: dict[str, str]
    universe_hash: str
    data_snapshot_hash: str
    reused: bool


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
    """Fetch the authoritative source candles without crossing a fixed cutoff."""

    # Normal rolling backtests intentionally end 72h before "now" so the
    # simulator can resolve trades through the configured holding horizon.
    # A fixed-period experiment is different: its end is the hard historical
    # cutoff. In that mode, never fetch or fingerprint candles after end_ms.
    fixed_period = bool(str(os.getenv("V11_BACKTEST_END_MS", "")).strip())
    raw_future_end = int(end_ms) if fixed_period else int(end_ms) + MAX_HOLD_MINUTES * 60_000

    def _complete_source_end(cutoff_ms: int, interval_ms: int) -> int:
        # API endpoints are inclusive by candle-open timestamp; the candle
        # opening exactly at cutoff_ms is still incomplete at that boundary.
        return int(cutoff_ms) - int(interval_ms)

    end_1d = _complete_source_end(raw_future_end, ONE_DAY_MS)
    end_4h = _complete_source_end(raw_future_end, FOUR_HOURS_MS)
    end_1h = _complete_source_end(raw_future_end, ONE_HOUR_MS)

    c1d_raw, c4_raw, c1_raw = await asyncio.gather(
        _fetch_paged_range(
            client,
            symbol,
            "Day1",
            start_ms - WARMUP_1D,
            end_1d,
        ),
        _fetch_paged_range(
            client,
            symbol,
            "Hour4",
            start_ms - WARMUP_4H,
            end_4h,
        ),
        _fetch_paged_range(
            client,
            symbol,
            "Min60",
            start_ms - WARMUP_1H,
            end_1h,
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
    tp_r_multiple: float | None = None,
    counterfactual_tp_r: float | None = None,
) -> SimulatedTrade | None:
    """Single authoritative 1H paper simulator used by the V11 Runner."""
    return simulate_trade(
        signal,
        future_candles,
        signal_close_time_ms=int(signal_close_time_ms),
        fee_rate=float(fee_rate),
        slippage_bps=float(slippage_bps),
        max_holding_minutes=max_holding_minutes,
        tp_r_multiple=tp_r_multiple,
        counterfactual_tp_r=counterfactual_tp_r,
    )


# ============================================================
# BACKTEST RUNNER
# ============================================================

class BacktestRunner:
    """Causal paper backtester for the V11 1D/12H/4H/1H engine."""

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
        tp_config: BacktestTPConfig | None = None,
    ) -> tuple[list[SimulatedTrade], dict[str, int], int, int, int]:
        """Generate the TP-independent accepted signal set, then simulate it.

        IMPORTANT: TP mode never participates in signal generation or signal
        eligibility. This is the controlled A/B invariant: CONTROL, 1.5R, 2R
        and 2.5R all receive the same engine-generated signal population.
        """
        symbol_started = time.monotonic()
        trades: list[SimulatedTrade] = []
        diagnostics: dict[str, int] = {}
        data_errors = 0
        simulation_errors = 0
        engine_errors = 0
        accepted_signals: list[tuple[int, dict[str, Any]]] = []
        seen_structures: set[tuple[Any, ...]] = set()
        seen_first_failures: set[tuple[str, str, str]] = set()
        tp_config = tp_config or BacktestTPConfig()

        def inc(key: str, amount: int = 1) -> None:
            diagnostics[key] = diagnostics.get(key, 0) + int(amount)

        # -----------------------------
        # PHASE 1: SIGNAL GENERATION
        # -----------------------------
        for row in history.candles_1h:
            signal_open_ms = _candle_time(row)
            signal_close_ms = signal_open_ms + ONE_HOUR_MS
            if signal_close_ms <= start_ms or signal_close_ms > end_ms:
                continue

            c1d = _closed_candles(history.candles_1d, history.times_1d, signal_close_ms, ONE_DAY_MS)
            c12 = _closed_candles(history.candles_12h, history.times_12h, signal_close_ms, TWELVE_HOURS_MS)
            c4 = _closed_candles(history.candles_4h, history.times_4h, signal_close_ms, FOUR_HOURS_MS)
            c1 = _closed_candles(history.candles_1h, history.times_1h, signal_close_ms, ONE_HOUR_MS)

            if len(c1d) < 210 or len(c12) < 60 or len(c4) < 180 or len(c1) < 180:
                inc("WARMUP_SKIPPED")
                continue

            btc_context = btc_context_cache.get(signal_close_ms) if btc_context_cache is not None else None
            try:
                analysis = analyze_candles(
                    history.symbol,
                    c1d,
                    c12,
                    c4,
                    c1,
                    now_ms=signal_close_ms,
                    btc_context=btc_context,
                    estimated_round_trip_cost_pct=float(
                        getattr(self.settings, "estimated_round_trip_cost_pct", 0.0015)
                    ),
                )
                inc("ENGINE_CALLS")
                inc("CANDLES_EVALUATED")
                inc("ENGINE_SUCCESS")
            except ValueError as exc:
                data_errors += 1
                inc("ENGINE_DATA_ERRORS")
                normalized = (str(exc).strip() or "ValueError").upper().replace(" ", "_").replace(":", "").replace("/", "_")[:120]
                inc(f"ENGINE_DATA_QUALITY_{normalized}")
                LOGGER.warning(
                    "BACKTEST DATA_QUALITY_ERROR | symbol=%s signal_close_ms=%s reason=%s",
                    history.symbol, signal_close_ms, exc,
                )
                continue
            except Exception as exc:
                engine_errors += 1
                inc("ENGINE_ERRORS")
                inc(f"ENGINE_ERROR_{type(exc).__name__}")
                LOGGER.exception(
                    "BACKTEST ENGINE_ERROR | symbol=%s signal_close_ms=%s reason=%s",
                    history.symbol, signal_close_ms, exc,
                )
                continue

            if analysis.get("btc_filter_ok") is False:
                inc("BTC_WOULD_BLOCK")

            if not analysis.get("technical_candidate"):
                inc("TECHNICAL_REJECT")
                for side_diag in analysis.get("side_diagnostics") or []:
                    side_name = str(side_diag.get("side") or "UNKNOWN").upper()
                    if side_name not in {"LONG", "SHORT"}:
                        continue
                    inc(f"SIDE_EVALUATIONS_{side_name}")
                    if side_diag.get("candidate"):
                        inc(f"SIDE_CANDIDATE_{side_name}")
                        continue
                    inc(f"FIRST_FAILURE_{side_name}_TOTAL")
                    reason = str(side_diag.get("primary_failure") or "rejected")
                    normalized_first = reason.upper().replace(" ", "_").replace(":", "").replace("/", "_")[:160]
                    inc(f"FIRST_FAILURE_{side_name}_{normalized_first}")
                    diag_key = str(side_diag.get("diagnostic_key") or "")
                    unique_key = (side_name, normalized_first, diag_key)
                    if unique_key not in seen_first_failures:
                        seen_first_failures.add(unique_key)
                        inc(f"UNIQUE_FIRST_FAILURE_{side_name}_{normalized_first}")
                failures = analysis.get("technical_gate_failures") or [analysis.get("rejection_stage") or "technical_candidate"]
                for failure in failures[:8]:
                    key = "REJECT_" + str(failure).upper().replace(" ", "_").replace(":", "").replace("/", "_")
                    inc(key)
                continue

            inc("TECHNICAL_ACCEPT")
            for side_diag in analysis.get("side_diagnostics") or []:
                side_name = str(side_diag.get("side") or "UNKNOWN").upper()
                if side_name in {"LONG", "SHORT"}:
                    inc(f"SIDE_EVALUATIONS_{side_name}")
                    if side_diag.get("candidate"):
                        inc(f"SIDE_CANDIDATE_{side_name}")
                    else:
                        inc(f"FIRST_FAILURE_{side_name}_TOTAL")
                        reason = str(side_diag.get("primary_failure") or "rejected")
                        normalized_first = reason.upper().replace(" ", "_").replace(":", "").replace("/", "_")[:160]
                        inc(f"FIRST_FAILURE_{side_name}_{normalized_first}")
                        diag_key = str(side_diag.get("diagnostic_key") or "")
                        unique_key = (side_name, normalized_first, diag_key)
                        if unique_key not in seen_first_failures:
                            seen_first_failures.add(unique_key)
                            inc(f"UNIQUE_FIRST_FAILURE_{side_name}_{normalized_first}")

            side = str(analysis.get("setup") or analysis.get("setup_candidate") or "").upper()
            if side not in {"LONG", "SHORT"}:
                inc("INVALID_ENGINE_SIDE")
                continue

            structure_key = (
                side,
                analysis.get("setup_impulse_high_time"),
                analysis.get("setup_impulse_low_time"),
                analysis.get("swept_level_1h"),
                analysis.get("reclaim_time_1h") or analysis.get("setup_retest_1h_time"),
            )
            if structure_key in seen_structures:
                inc("DUPLICATE_STRUCTURE_SKIPPED")
                continue
            seen_structures.add(structure_key)
            accepted_signals.append((signal_close_ms, analysis))
            inc("SIGNAL_SET_ACCEPTED")

        # -----------------------------
        # PHASE 2: EXIT-ONLY SIMULATION
        # -----------------------------
        diagnostics["SIGNAL_GENERATION_COMPLETE"] = 1
        diagnostics["TP_MODE_EXIT_ONLY"] = 1
        diagnostics["SIGNALS_READY_FOR_TP_SIMULATION"] = len(accepted_signals)
        for signal_close_ms, analysis in accepted_signals:
            future = _future_candles(history.candles_1h, history.times_1h, signal_close_ms)
            if not future:
                inc("NO_FUTURE_CANDLES")
                continue

            max_hold = float(
                _safe_number(analysis.get("intraday_max_hold_minutes"))
                or getattr(self.settings, "backtest_max_holding_minutes", MAX_HOLD_MINUTES)
            )
            fee_rate = float(
                _safe_number(getattr(self.settings, "backtest_fee_rate", DEFAULT_FEE_RATE))
                or DEFAULT_FEE_RATE
            )
            slippage_bps = float(
                _safe_number(getattr(self.settings, "backtest_slippage_bps", DEFAULT_SLIPPAGE_BPS))
                or DEFAULT_SLIPPAGE_BPS
            )
            try:
                trade = simulate_trade_1h(
                    analysis,
                    future,
                    signal_close_time_ms=signal_close_ms,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    max_holding_minutes=max_hold,
                    tp_r_multiple=tp_config.r_multiple,
                )
            except Exception as exc:
                simulation_errors += 1
                inc("SIMULATION_ERRORS")
                LOGGER.exception(
                    "BACKTEST SIMULATION_ERROR | symbol=%s signal_close_ms=%s reason=%s",
                    history.symbol, signal_close_ms, exc,
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
            if trade.outcome == "OPEN":
                inc("OPEN_AT_END")

        diagnostics["CANDIDATES_ENGINE_EVALUATED"] = diagnostics.get("ENGINE_CALLS", 0)
        diagnostics["ENGINE_OUTCOME_UNKNOWN_CALLS"] = 0
        diagnostics["TECHNICAL_ACCOUNTING_GAP"] = max(
            0,
            diagnostics.get("ENGINE_SUCCESS", 0)
            - diagnostics.get("TECHNICAL_ACCEPT", 0)
            - diagnostics.get("TECHNICAL_REJECT", 0),
        )
        diagnostics["ANALYSIS_TOTAL_TIME_MS"] = int((time.monotonic() - symbol_started) * 1000)
        diagnostics["V11_MIN_IMPULSE_ATR_X100"] = int(round(float(V11_MIN_IMPULSE_ATR) * 100))
        diagnostics["V11_MIN_IMPULSE_ATR_ACTUAL_X100"] = int(round(float(V11_MIN_IMPULSE_ATR) * 100))
        diagnostics["V11_MAX_TRIGGER_BARS"] = int(V11_MAX_TRIGGER_BARS)
        diagnostics["V11_SL_MODE_4H_ORIGIN"] = 1 if str(V11_SL_MODE).upper() == "4H_ORIGIN" else 0
        diagnostics["V11_SHORT_RANGE_RELAXED"] = 1 if bool(V11_SHORT_RANGE_RELAXED) else 0
        diagnostics["MAE_USES_BAR_EXTREMES"] = 1
        diagnostics["TP_SIGNAL_COUPLING_REMOVED"] = 1
        diagnostics["FIXED_PERIOD_HARD_CUTOFF"] = 1 if str(os.getenv("V11_BACKTEST_END_MS", "")).strip() else 0

        reject_items = [
            (key, int(value)) for key, value in diagnostics.items()
            if key.startswith("REJECT_") and int(value) > 0
        ]
        top_reject = max(reject_items, key=lambda item: item[1])[0] if reject_items else "NONE"
        LOGGER.info(
            "BACKTEST SYMBOL COMPLETE | symbol=%s engine_calls=%d technical_accept=%d technical_reject=%d "
            "signals_ready=%d top_reject=%s trades=%d data_errors=%d engine_errors=%d simulation_errors=%d seconds=%.2f",
            history.symbol,
            diagnostics.get("ENGINE_CALLS", 0),
            diagnostics.get("TECHNICAL_ACCEPT", 0),
            diagnostics.get("TECHNICAL_REJECT", 0),
            diagnostics.get("SIGNALS_READY_FOR_TP_SIMULATION", 0),
            top_reject,
            len(trades),
            data_errors,
            engine_errors,
            simulation_errors,
            time.monotonic() - symbol_started,
        )
        return trades, diagnostics, data_errors, simulation_errors, engine_errors

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

    async def run(self, days: int, *, tp_mode: str = "CONTROL") -> BacktestSummary:
        """Run a causal paper backtest on a frozen, integrity-verified dataset."""
        days = int(days)
        tp_config = BacktestTPConfig.from_mode(tp_mode)
        if days not in SUPPORTED_BACKTEST_DAYS:
            raise ValueError("Supported backtests: 1D, 7D, 30D, 60D, 90D, 180D, 365D")
        if self._running or self._run_lock.locked():
            raise BacktestAlreadyRunning("A backtest is already running. Please wait for it to finish.")

        async with self._run_lock:
            self._running = True
            started = time.monotonic()
            stop_event = asyncio.Event()
            state: dict[str, Any] = {
                "phase": "INITIALIZING", "total": 0, "processed": 0, "tested": 0,
                "data_errors": 0, "engine_errors": 0, "simulation_errors": 0,
                "analysis_errors": 0, "signals": 0, "symbol": "-",
            }
            heartbeat = asyncio.create_task(
                self._heartbeat(state, started, stop_event, days),
                name="backtest-heartbeat",
            )
            diagnostics: dict[str, int] = {}
            trades: list[SimulatedTrade] = []

            def inc_global(key: str, amount: int = 1) -> None:
                diagnostics[key] = diagnostics.get(key, 0) + int(amount)

            snapshot: BacktestSnapshot | None = None
            snapshot_ready = False
            try:
                start_ms, end_ms = self._period(days)
                max_symbols = max(1, min(
                    int(getattr(self.settings, "backtest_max_symbols", MAX_BACKTEST_SYMBOLS)),
                    MAX_BACKTEST_SYMBOLS,
                ))
                strategy_fingerprint = _strategy_fingerprint(self.settings)
                universe_selection_fp = _universe_selection_fingerprint(self.universe)
                snapshot_id = _snapshot_id(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    max_symbols=max_symbols,
                    strategy_fingerprint=strategy_fingerprint,
                    universe_selection_fingerprint=universe_selection_fp,
                )
                root = _snapshot_root(self.settings)
                snapshot_dir = root / snapshot_id
                manifest_path = snapshot_dir / "manifest.json"
                manifest = _load_manifest(manifest_path)

                if manifest is not None and str(manifest.get("status", "")).upper() == "READY":
                    symbols, expected_data_hashes, universe_hash, data_snapshot_hash = _validate_ready_manifest(
                        manifest,
                        snapshot_id=snapshot_id,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        max_symbols=max_symbols,
                        strategy_fingerprint=strategy_fingerprint,
                    )
                    snapshot = BacktestSnapshot(
                        snapshot_id=snapshot_id,
                        directory=snapshot_dir,
                        symbols=tuple(symbols),
                        expected_data_hashes=expected_data_hashes,
                        universe_hash=universe_hash,
                        data_snapshot_hash=data_snapshot_hash,
                        reused=True,
                    )
                    snapshot_ready = True
                    inc_global("BACKTEST_SNAPSHOT_REUSED", 1)
                    LOGGER.info(
                        "BACKTEST SNAPSHOT REUSED | id=%s symbols=%d universe_hash=%s data_hash=%s",
                        snapshot_id, len(symbols), universe_hash[:16], data_snapshot_hash[:16],
                    )
                else:
                    # A partial/failed snapshot is never silently reused. The next
                    # clean run rebuilds the universe and fingerprints from scratch.
                    if snapshot_dir.exists():
                        import shutil
                        shutil.rmtree(snapshot_dir, ignore_errors=True)
                    state["phase"] = "UNIVERSE"
                    all_symbols = list(await asyncio.wait_for(self.universe.refresh(), timeout=FETCH_TIMEOUT_SECONDS))
                    symbols = _canonical_symbols(all_symbols, max_symbols)
                    if not symbols:
                        inc_global("NO_ELIGIBLE_SYMBOLS", 1)
                        summary = summarize(
                            days=days,
                            coins_selected=0,
                            coins_tested=0,
                            data_errors=0,
                            period_start_ms=start_ms,
                            period_end_ms=end_ms,
                            tp_mode=tp_config.mode,
                            execution_errors=0,
                            rejected_setups=0,
                            trades=[],
                            diagnostics={**diagnostics, "CURRENT_UNIVERSE_SNAPSHOT_BIAS": 1},
                        )
                        return summary
                    universe_hash = _sha256_json(symbols)
                    snapshot_dir.mkdir(parents=True, exist_ok=True)
                    _atomic_write_json(
                        manifest_path,
                        {
                            "schema": BACKTEST_SNAPSHOT_SCHEMA,
                            "status": "BUILDING",
                            "snapshot_id": snapshot_id,
                            "period_start_ms": int(start_ms),
                            "period_end_ms": int(end_ms),
                            "max_symbols": int(max_symbols),
                            "strategy_fingerprint": strategy_fingerprint,
                            "universe_selection_fingerprint": universe_selection_fp,
                            "universe_source": "CURRENT_LIVE_RANKING_SNAPSHOT",
                            "symbols": symbols,
                            "universe_hash": universe_hash,
                            "data_hashes": {},
                        },
                    )
                    snapshot = BacktestSnapshot(
                        snapshot_id=snapshot_id,
                        directory=snapshot_dir,
                        symbols=tuple(symbols),
                        expected_data_hashes={},
                        universe_hash=universe_hash,
                        data_snapshot_hash="",
                        reused=False,
                    )
                    inc_global("BACKTEST_SNAPSHOT_CREATED", 1)
                    LOGGER.info(
                        "BACKTEST SNAPSHOT CREATED | id=%s symbols=%d universe_hash=%s",
                        snapshot_id, len(symbols), universe_hash[:16],
                    )

                state["total"] = len(snapshot.symbols)
                inc_global("SYMBOLS_DISCOVERED", len(snapshot.symbols))
                inc_global("SYMBOLS_SELECTED", len(snapshot.symbols))
                state["phase"] = "BTC_DATA"

                # BTC is part of the frozen data set, even though it is not in
                # the selected altcoin universe. Its hash is required for strict A/B.
                btc_history: SymbolHistory | None = None
                try:
                    btc_history = await asyncio.wait_for(
                        self._fetch_btc_history(start_ms, end_ms),
                        timeout=FETCH_TIMEOUT_SECONDS,
                    )
                    btc_hash = _history_fingerprint(btc_history)
                    expected_btc_hash = snapshot.expected_data_hashes.get("BTC_USDT")
                    if snapshot_ready and expected_btc_hash and btc_hash != expected_btc_hash:
                        raise BacktestSnapshotError(
                            f"BTC historical data changed for frozen snapshot {snapshot.snapshot_id}"
                        )
                    inc_global("BTC_DATA_READY")
                    inc_global("BTC_DATA_FINGERPRINT_VERIFIED", 1 if snapshot_ready else 0)
                except BacktestSnapshotError:
                    raise
                except Exception as exc:
                    inc_global("BTC_DATA_ERROR")
                    LOGGER.exception("BACKTEST BTC DATA ERROR | reason=%s", exc)
                    raise BacktestSnapshotError(
                        "BTC historical data could not be fetched/fingerprinted; "
                        "the backtest cannot create or reuse a strict A/B dataset snapshot"
                    ) from exc

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
                    inc_global("BTC_CONTEXT_CACHE_ITEMS", len(btc_context_cache))

                state["phase"] = "SYMBOL_ANALYSIS"
                queue: asyncio.Queue[str | None] = asyncio.Queue()
                for symbol in snapshot.symbols:
                    queue.put_nowait(symbol)
                for _ in range(self.max_concurrency):
                    queue.put_nowait(None)
                state_lock = asyncio.Lock()
                observed_data_hashes: dict[str, str] = {"BTC_USDT": btc_hash} if btc_hash else {}

                async def worker(worker_id: int) -> None:
                    while True:
                        symbol = await queue.get()
                        try:
                            if symbol is None:
                                return
                            state["symbol"] = symbol
                            state["phase"] = "FETCH_HISTORY"
                            try:
                                # BTC_USDT is fetched once above because it is
                                # also used for the causal macro context. If BTC
                                # happens to be inside the frozen universe, reuse
                                # that exact history instead of fetching it a
                                # second time. This avoids duplicate I/O and, more
                                # importantly, avoids treating BTC as two distinct
                                # fingerprint records during snapshot validation.
                                if symbol == "BTC_USDT" and btc_history is not None:
                                    history = btc_history
                                else:
                                    history = await asyncio.wait_for(
                                        self._fetch_history(symbol, start_ms, end_ms),
                                        timeout=FETCH_TIMEOUT_SECONDS,
                                    )
                                symbol_hash = _history_fingerprint(history)
                                expected_hash = snapshot.expected_data_hashes.get(symbol)
                                if snapshot_ready and expected_hash and symbol_hash != expected_hash:
                                    raise BacktestSnapshotError(
                                        f"Historical data changed for frozen snapshot {snapshot.snapshot_id}: {symbol}"
                                    )
                                observed_data_hashes[symbol] = symbol_hash
                                async with state_lock:
                                    inc_global("SYMBOLS_DATA_READY")
                                    state["tested"] += 1
                            except BacktestSnapshotError:
                                raise
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                async with state_lock:
                                    state["data_errors"] += 1
                                    inc_global("SYMBOLS_DATA_ERROR")
                                    inc_global(f"DATA_ERROR_{type(exc).__name__}")
                                LOGGER.exception(
                                    "BACKTEST DATA ERROR | worker=%d symbol=%s reason=%s",
                                    worker_id, symbol, exc,
                                )
                                continue

                            data_started = time.monotonic()
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
                                        tp_config,
                                    ),
                                    timeout=max(
                                        1.0,
                                        float(getattr(self.settings, "backtest_analysis_timeout_seconds", 120.0)),
                                    ),
                                )
                            except asyncio.CancelledError:
                                raise
                            except asyncio.TimeoutError as exc:
                                async with state_lock:
                                    state["analysis_errors"] += 1
                                    inc_global("ANALYSIS_TIMEOUT")
                                LOGGER.error(
                                    "BACKTEST ANALYSIS TIMEOUT | worker=%d symbol=%s reason=%s",
                                    worker_id, symbol, exc,
                                )
                                continue
                            except Exception as exc:
                                async with state_lock:
                                    state["analysis_errors"] += 1
                                    inc_global("SYMBOLS_ANALYSIS_FAILED")
                                    inc_global(f"ANALYSIS_ERROR_{type(exc).__name__}")
                                LOGGER.exception(
                                    "BACKTEST ANALYSIS ERROR | worker=%d symbol=%s reason=%s",
                                    worker_id, symbol, exc,
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
                                "BACKTEST PROGRESS | worker=%d days=%d processed=%d/%d tested=%d signals=%d "
                                "data_errors=%d engine_errors=%d simulation_errors=%d symbol=%s duration_symbol=%.2fs",
                                worker_id, days, state["processed"], len(snapshot.symbols), state["tested"], len(trades),
                                state["data_errors"], state["engine_errors"], state["simulation_errors"], symbol,
                                time.monotonic() - data_started,
                            )
                        finally:
                            state["processed"] += 1 if symbol is not None else 0
                            queue.task_done()

                workers = [
                    asyncio.create_task(worker(index), name=f"backtest-worker-{index}")
                    for index in range(self.max_concurrency)
                ]
                worker_results = await asyncio.gather(*workers, return_exceptions=True)
                for result in worker_results:
                    if isinstance(result, BacktestSnapshotError):
                        raise result
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result

                trades.sort(key=lambda trade: (int(trade.signal_time_ms), trade.symbol, trade.side))

                # A new snapshot becomes READY only after every selected symbol
                # and BTC dataset has been fetched successfully and fingerprinted.
                if not snapshot_ready:
                    # BTC_USDT can legitimately be part of the selected universe
                    # as well as the mandatory macro-context dataset. Compare key
                    # identity sets rather than adding the two counts, otherwise
                    # a valid 200-symbol snapshot containing BTC is incorrectly
                    # rejected because the BTC hash is shared by both roles.
                    expected_fingerprint_keys = set(snapshot.symbols)
                    if btc_hash:
                        expected_fingerprint_keys.add("BTC_USDT")
                    observed_fingerprint_keys = set(observed_data_hashes)
                    if observed_fingerprint_keys != expected_fingerprint_keys:
                        missing = sorted(expected_fingerprint_keys - observed_fingerprint_keys)
                        unexpected = sorted(observed_fingerprint_keys - expected_fingerprint_keys)
                        LOGGER.error(
                            "BACKTEST SNAPSHOT FINGERPRINT INCOMPLETE | expected=%d observed=%d missing=%s unexpected=%s",
                            len(expected_fingerprint_keys),
                            len(observed_fingerprint_keys),
                            missing[:10],
                            unexpected[:10],
                        )
                        raise BacktestSnapshotError(
                            "Backtest dataset could not be fully fingerprinted; "
                            f"missing={missing[:10]} unexpected={unexpected[:10]}"
                        )
                    final_data_hash = _data_snapshot_hash(observed_data_hashes)
                    _atomic_write_json(
                        manifest_path,
                        {
                            "schema": BACKTEST_SNAPSHOT_SCHEMA,
                            "status": "READY",
                            "snapshot_id": snapshot.snapshot_id,
                            "period_start_ms": int(start_ms),
                            "period_end_ms": int(end_ms),
                            "max_symbols": int(max_symbols),
                            "strategy_fingerprint": strategy_fingerprint,
                            "universe_selection_fingerprint": universe_selection_fp,
                            "universe_source": "CURRENT_LIVE_RANKING_SNAPSHOT",
                            "symbols": list(snapshot.symbols),
                            "universe_hash": snapshot.universe_hash,
                            "data_hashes": observed_data_hashes,
                            "data_snapshot_hash": final_data_hash,
                            "created_at_utc": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    snapshot = BacktestSnapshot(
                        snapshot_id=snapshot.snapshot_id,
                        directory=snapshot.directory,
                        symbols=snapshot.symbols,
                        expected_data_hashes=observed_data_hashes,
                        universe_hash=snapshot.universe_hash,
                        data_snapshot_hash=final_data_hash,
                        reused=False,
                    )

                state["phase"] = "FINALIZING"
                rejected_setups = int(diagnostics.get("TECHNICAL_REJECT", 0))
                execution_errors = int(
                    state.get("engine_errors", 0)
                    + state.get("simulation_errors", 0)
                    + state.get("analysis_errors", 0)
                )
                diagnostics["BACKTEST_SNAPSHOT_VERIFIED"] = 1
                diagnostics["BACKTEST_UNIVERSE_HASH_PRESENT"] = 1 if snapshot.universe_hash else 0
                diagnostics["BACKTEST_DATA_HASH_PRESENT"] = 1 if snapshot.data_snapshot_hash else 0
                diagnostics["TP_OVERRIDE_EXIT_ONLY"] = 1
                diagnostics["SIGNAL_GENERATION_TP_INDEPENDENT"] = 1
                diagnostics["MAE_BAR_EXTREME_DEFINITION"] = 1
                diagnostics["OPEN_AT_BACKTEST_END"] = sum(1 for t in trades if str(t.outcome).upper() == "OPEN")

                summary = summarize(
                    days=days,
                    coins_selected=len(snapshot.symbols),
                    coins_tested=int(state["tested"]),
                    data_errors=int(state["data_errors"]),
                    period_start_ms=int(start_ms),
                    period_end_ms=int(end_ms),
                    tp_mode=tp_config.mode,
                    execution_errors=execution_errors,
                    rejected_setups=rejected_setups,
                    trades=trades,
                    diagnostics=diagnostics,
                    snapshot_id=snapshot.snapshot_id,
                    universe_hash=snapshot.universe_hash,
                    data_snapshot_hash=snapshot.data_snapshot_hash,
                    snapshot_status="REUSED" if snapshot.reused else "CREATED",
                )
                LOGGER.info(
                    "BACKTEST COMPLETE | days=%d tested=%d/%d signals=%d data_errors=%d engine_errors=%d "
                    "simulation_errors=%d analysis_errors=%d snapshot=%s id=%s duration=%.2fs",
                    days,
                    state["tested"],
                    len(snapshot.symbols),
                    len(trades),
                    state["data_errors"],
                    state["engine_errors"],
                    state["simulation_errors"],
                    state["analysis_errors"],
                    "REUSED" if snapshot.reused else "CREATED",
                    snapshot.snapshot_id,
                    time.monotonic() - started,
                )
                return summary
            finally:
                stop_event.set()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
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
    "BacktestSnapshotError",
    "BacktestRunner",
    "BacktestTPConfig",
    "SymbolHistory",
    "build_12h_candles",
    "simulate_trade_1h",
]
