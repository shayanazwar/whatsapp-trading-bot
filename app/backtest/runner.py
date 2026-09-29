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
                first_index = bisect_right(open_times, retest_time - 1)
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
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

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
        candidate_times = self._find_15m_setup_windows(c15, start, end, diagnostics)
        if not candidate_times:
            diagnostics["FIVE_MIN_FETCH_SKIPPED"] += 1
            diagnostics["ONE_D_FETCH_SKIPPED"] += 1
            return SymbolHistory(symbol, c4_raw, c1_raw, c15_raw, [], [], candidate_times, dict(diagnostics))

        t5 = asyncio.create_task(_fetch_timeframe(self.client, symbol, "5M", INTERVALS["5m"], starts["5m"], end))
        t1d = asyncio.create_task(_fetch_timeframe(self.client, symbol, "1D", INTERVALS["1d"], starts["1d"], end))
        try:
            c5_raw, c1d_raw = await asyncio.wait_for(asyncio.gather(t5, t1d), timeout=SYMBOL_FETCH_TIMEOUT_SECONDS)
        except BaseException:
            for task in (t5, t1d):
                if not task.done():
                    task.cancel()
            await asyncio.gather(t5, t1d, return_exceptions=True)
            raise
        diagnostics["FIVE_MIN_FETCH"] += 1
        diagnostics["ONE_D_FETCH"] += 1
        return SymbolHistory(symbol, c4_raw, c1_raw, c15_raw, c5_raw, c1d_raw, candidate_times, dict(diagnostics))

    def _backtest_symbol(self, history: SymbolHistory, start: int, end: int, btc_history: SymbolHistory, btc_context_cache: dict | None = None) -> list[SimulatedTrade]:
        c4 = convert_candles(history.candles_4h); c1 = convert_candles(history.candles_1h); c15 = convert_candles(history.candles_15m)
        c5 = convert_candles(history.candles_5m); c1d = convert_candles(history.candles_1d)
        btc4 = convert_candles(btc_history.candles_4h); btc1 = convert_candles(btc_history.candles_1h); btc15 = convert_candles(btc_history.candles_15m)

        candidates = tuple(getattr(history, "prefilter_candidates", ()) or ())
        if not candidates:
            local_diag = defaultdict(int)
            candidates = self._find_15m_setup_windows(c15, start, end, local_diag)
            history.diagnostics = {**getattr(history, "diagnostics", {}), **dict(local_diag)}
        diagnostics = history.diagnostics

        # Make compatibility objects from tests safe without imposing runner decisions.
        required_diag_keys = ("FULL_ENGINE_CANDIDATES", "TECHNICAL_ACCEPT", "BTC_ACCEPT", "BTC_REJECT", "SIMULATION_ACCEPT", "SIMULATION_ERRORS", "ENGINE_ERRORS")
        for key in required_diag_keys:
            diagnostics[key] = int(diagnostics.get(key, 0))

        btc_times = ([_row_time(r) for r in btc4], [_row_time(r) for r in btc1], [_row_time(r) for r in btc15])
        if btc_context_cache is None:
            btc_context_cache = {}

        c5_times = [_row_time(r) for r in history.candles_5m]
        trades: list[SimulatedTrade] = []
        previous_exit_time: int | None = None
        seen_structures: set[tuple[Any, Any, Any]] = set()
        regime_cache: dict[int, dict[str, Any]] = {}
        alignment_cache: dict[tuple[int, str], dict[str, Any]] = {}

        for signal_close_time, setup_hint in candidates:
            signal_close_time = int(signal_close_time)
            if signal_close_time < start or signal_close_time > end:
                continue
            if previous_exit_time is not None and signal_close_time <= previous_exit_time:
                diagnostics["OVERLAPPING_SIGNAL_SKIPPED"] = diagnostics.get("OVERLAPPING_SIGNAL_SKIPPED", 0) + 1
                continue

            c4s = _closed_slice(c4, H4_MS, signal_close_time)
            c1s = _closed_slice(c1, H1_MS, signal_close_time)
            c15s = _closed_slice(c15, M15_MS, signal_close_time)
            c5s = _closed_slice(c5, M5_MS, signal_close_time)
            c1ds = _closed_slice(c1d, D1_MS, signal_close_time)

            # Cheap, cached higher-timeframe gate. The full engine remains
            # authoritative, but most 15M windows can be discarded here without
            # running the expensive complete indicator/target calculation.
            side_hint = str((setup_hint or {}).get("side") or "").upper()
            if side_hint not in {"LONG", "SHORT"} or len(c4s) < 205 or len(c1s) < 205:
                diagnostics["HTF_PREFILTER_REJECT"] = diagnostics.get("HTF_PREFILTER_REJECT", 0) + 1
                continue
            h4_key = _row_time(c4s[-1])
            regime = regime_cache.get(h4_key)
            if regime is None:
                regime = _four_hour_regime(c4s)
                regime_cache[h4_key] = regime
            h1_key = _row_time(c1s[-1])
            align_key = (h1_key, str(regime.get("regime") or "NO_TRADE"))
            alignment = alignment_cache.get(align_key)
            if alignment is None:
                alignment = _one_hour_alignment(c1s, regime)
                alignment_cache[align_key] = alignment
            if not ((side_hint == "LONG" and regime.get("bull") and alignment.get("long")) or (side_hint == "SHORT" and regime.get("bear") and alignment.get("short"))):
                diagnostics["HTF_PREFILTER_REJECT"] = diagnostics.get("HTF_PREFILTER_REJECT", 0) + 1
                continue

            # The full engine cannot produce LONG/SHORT unless the same 15M
            # entry-confirmation gate is ready. Evaluating this exact gate here
            # is therefore a semantics-preserving prefilter, not a strategy
            # shortcut. It avoids running expensive target/score calculations on
            # candles that the authoritative engine will necessarily reject.
            try:
                entry_check = _fifteen_minute_entry_confirmation(
                    c15s,
                    side_hint,
                    float((setup_hint or {}).get("bos_level")),
                    int((setup_hint or {}).get("retest_time")),
                )
            except Exception:
                # The entry check is an optimization only. If it cannot be
                # evaluated, keep the candidate and let the authoritative full
                # engine decide. This prevents the prefilter from changing the
                # strategy result because of a helper/test-data mismatch.
                diagnostics["ENTRY_PREFILTER_ERRORS"] = diagnostics.get("ENTRY_PREFILTER_ERRORS", 0) + 1
                entry_check = {"ready": True}

            if not entry_check.get("ready"):
                # This gate is used only as a cheap optimization. A negative
                # result is not authoritative because the full engine is the
                # sole source of truth for the final technical decision.
                diagnostics["ENTRY_PREFILTER_BYPASS"] = diagnostics.get("ENTRY_PREFILTER_BYPASS", 0) + 1

            diagnostics["FULL_ENGINE_CANDIDATES"] += 1

            try:
                analysis = analyze_candles(history.symbol, c4s, c1s, c15s, c5s, c1ds, now_ms=signal_close_time)
            except Exception:
                diagnostics["ENGINE_ERRORS"] = diagnostics.get("ENGINE_ERRORS", 0) + 1
                diagnostics["ENGINE_EXCEPTION"] = diagnostics.get("ENGINE_EXCEPTION", 0) + 1
                LOGGER.exception("BACKTEST ENGINE failed | %s | signal=%d", history.symbol, signal_close_time)
                continue

            if not analysis.get("technical_candidate"):
                diagnostics["TECHNICAL_REJECT"] = diagnostics.get("TECHNICAL_REJECT", 0) + 1
                for reason in analysis.get("technical_gate_failures", []):
                    diagnostics[f"ENGINE_REJECT_{reason}"] = diagnostics.get(f"ENGINE_REJECT_{reason}", 0) + 1
                continue

            diagnostics["TECHNICAL_ACCEPT"] = diagnostics.get("TECHNICAL_ACCEPT", 0) + 1
            side = str(analysis.get("setup") or "").upper()
            diagnostics[f"FULL_ENGINE_ACCEPT_{side}"] = diagnostics.get(f"FULL_ENGINE_ACCEPT_{side}", 0) + 1
            structure_key = (side, analysis.get("setup_bos_time"), analysis.get("setup_retest_time"))
            if structure_key in seen_structures:
                diagnostics["DUPLICATE_STRUCTURE_SKIPPED"] = diagnostics.get("DUPLICATE_STRUCTURE_SKIPPED", 0) + 1
                continue
            seen_structures.add(structure_key)

            btc_c4_end = bisect_right(btc_times[0], signal_close_time - H4_MS)
            btc_c1_end = bisect_right(btc_times[1], signal_close_time - H1_MS)
            btc_c15_end = bisect_right(btc_times[2], signal_close_time - M15_MS)
            btc_key = (btc_c4_end, btc_c1_end, btc_c15_end)
            cached = btc_context_cache.get(btc_key)
            try:
                if cached is None:
                    context = build_btc_context(btc4[:btc_c4_end], btc1[:btc_c1_end], btc15[:btc_c15_end])
                    ok, reason = btc_filter_ok(side, context, is_btc=(history.symbol.upper() == "BTC_USDT"))
                    btc_context_cache[btc_key] = (context, reason)
                else:
                    context, _reason = cached
                    ok, reason = btc_filter_ok(side, context, is_btc=(history.symbol.upper() == "BTC_USDT"))
            except Exception:
                diagnostics["BTC_CONTEXT_ERRORS"] = diagnostics.get("BTC_CONTEXT_ERRORS", 0) + 1
                LOGGER.exception("BACKTEST BTC filter failed | %s | signal=%d", history.symbol, signal_close_time)
                continue

            if not ok:
                diagnostics["BTC_REJECT"] = diagnostics.get("BTC_REJECT", 0) + 1
                diagnostics[f"BTC_REJECT_{side}"] = diagnostics.get(f"BTC_REJECT_{side}", 0) + 1
                continue
            diagnostics["BTC_ACCEPT"] = diagnostics.get("BTC_ACCEPT", 0) + 1
            diagnostics[f"BTC_ACCEPT_{side}"] = diagnostics.get(f"BTC_ACCEPT_{side}", 0) + 1

            future_start = bisect_right(c5_times, signal_close_time - 1)
            future_end = bisect_right(c5_times, end - 1)
            future_candles = history.candles_5m[future_start:future_end]
            if not future_candles:
                diagnostics["NO_FUTURE_CANDLES"] = diagnostics.get("NO_FUTURE_CANDLES", 0) + 1
                continue

            fee_rate = float(getattr(self.settings, "backtest_fee_rate", DEFAULT_FEE_RATE) if self.settings is not None else DEFAULT_FEE_RATE)
            slippage_bps = float(getattr(self.settings, "backtest_slippage_bps", DEFAULT_SLIPPAGE_BPS) if self.settings is not None else DEFAULT_SLIPPAGE_BPS)
            max_hold = float(analysis.get("intraday_max_hold_minutes") or getattr(self.settings, "backtest_max_holding_minutes", DEFAULT_MAX_HOLDING_MINUTES) if self.settings is not None else DEFAULT_MAX_HOLDING_MINUTES)
            try:
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
                LOGGER.exception("BACKTEST SIMULATION failed | %s | signal=%d", history.symbol, signal_close_time)
                continue

            if trade is None:
                diagnostics["SIMULATION_NO_TRADE"] = diagnostics.get("SIMULATION_NO_TRADE", 0) + 1
                continue

            trades.append(trade)
            diagnostics["SIMULATION_ACCEPT"] = diagnostics.get("SIMULATION_ACCEPT", 0) + 1
            diagnostics[f"OUTCOME_{trade.outcome}"] = diagnostics.get(f"OUTCOME_{trade.outcome}", 0) + 1
            if trade.outcome == "TP2":
                diagnostics["TP2_BEFORE_SL"] = diagnostics.get("TP2_BEFORE_SL", 0) + 1
            if trade.outcome == "SL":
                diagnostics["SL_OUTCOME"] = diagnostics.get("SL_OUTCOME", 0) + 1
            if trade.expired:
                diagnostics["EXPIRY"] = diagnostics.get("EXPIRY", 0) + 1
            previous_exit_time = trade.exit_time_ms

        return trades

    async def run(self, days: int) -> BacktestSummary:
        days = int(days)
        if days not in {7, 30, 90}:
            raise ValueError("Supported backtests: 7D, 30D, 90D")
        if self._lock.locked():
            raise BacktestAlreadyRunning("A backtest is already running. Please wait for it to finish.")

        async with self._lock:
            started = time.monotonic()
            stop_event = asyncio.Event()
            state: dict[str, Any] = {
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
            heartbeat = asyncio.create_task(
                self._heartbeat(state, started, stop_event, days),
                name="backtest-heartbeat",
            )
            diagnostics: defaultdict[str, int] = defaultdict(int)
            trades: list[SimulatedTrade] = []

            try:
                period_end = (int(time.time() * 1000) // M5_MS) * M5_MS
                period_start = period_end - days * 24 * 60 * 60 * 1000

                state["phase"] = "UNIVERSE"
                try:
                    symbols = list(await asyncio.wait_for(self.universe.refresh(), timeout=60))[:300]
                except Exception:
                    state["data_errors"] += 1
                    raise
                if not symbols:
                    raise RuntimeError("No eligible MEXC Futures symbols are available for backtesting.")
                state["total"] = len(symbols)
                LOGGER.info("BACKTEST START | days=%d symbols=%d start=%d end=%d", days, len(symbols), period_start, period_end)

                state["phase"] = "BTC DATA"
                try:
                    btc_history = await self._fetch_btc_history(period_start, period_end)
                except Exception:
                    state["data_errors"] += 1
                    diagnostics["BTC_DATA_ERROR"] += 1
                    raise
                btc_context_cache: dict = {}

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
                            state["worker"] = worker_id
                            state["symbol"] = symbol
                            state["phase"] = "PREFILTER"

                            data_started = time.monotonic()
                            try:
                                history = btc_history if str(symbol).upper() == "BTC_USDT" else await self._prepare_symbol_history(symbol, period_start, period_end)
                                for key, value in history.diagnostics.items():
                                    diagnostics[key] += int(value)
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                async with state_lock:
                                    state["data_errors"] += 1
                                diagnostics[f"DATA_ERROR_{type(exc).__name__}"] += 1
                                LOGGER.exception("BACKTEST DATA ERROR | %s | %s", symbol, exc)
                                continue

                            data_seconds = time.monotonic() - data_started
                            async with state_lock:
                                state["data_seconds"] += data_seconds
                            state["phase"] = "ANALYSIS"
                            analysis_started = time.monotonic()
                            pre_analysis_diag = dict(history.diagnostics)
                            try:
                                symbol_trades, symbol_diag = await self._run_symbol_analysis_with_timeout(
                                    history,
                                    period_start,
                                    period_end,
                                    btc_history,
                                )
                                # The isolated process mutates a private copy of history.
                                # Merge only the delta produced during analysis so the
                                # prefilter diagnostics already counted above are not doubled.
                                for key, value in symbol_diag.items():
                                    delta = int(value) - int(pre_analysis_diag.get(key, 0))
                                    if delta > 0:
                                        diagnostics[key] += delta
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                async with state_lock:
                                    state["engine_errors"] += 1
                                diagnostics[f"ANALYSIS_ERROR_{type(exc).__name__}"] += 1
                                if isinstance(exc, TimeoutError):
                                    diagnostics["ANALYSIS_TIMEOUT"] += 1
                                    LOGGER.error("BACKTEST ANALYSIS TIMEOUT | %s | %s", symbol, exc)
                                else:
                                    LOGGER.exception("BACKTEST ANALYSIS ERROR | %s | %s", symbol, exc)
                                continue

                            analysis_seconds = time.monotonic() - analysis_started
                            async with state_lock:
                                state["analysis_seconds"] += analysis_seconds
                                state["last_symbol_seconds"] = data_seconds + analysis_seconds
                            trades.extend(symbol_trades)
                            async with state_lock:
                                state["tested"] += 1
                                state["signals"] = len(trades)
                            LOGGER.info(
                                "BACKTEST PROGRESS | days=%d processed=%d/%d tested=%d data_errors=%d engine_errors=%d simulation_errors=%d signals=%d",
                                days, state["processed"], len(symbols), state["tested"], state["data_errors"], state["engine_errors"], state["simulation_errors"], len(trades),
                            )
                        finally:
                            if symbol is not None:
                                async with state_lock:
                                    state["processed"] += 1
                            queue.task_done()

                workers = [asyncio.create_task(worker(index), name=f"backtest-worker-{index}") for index in range(self.max_concurrency)]
                await asyncio.gather(*workers)

                state["phase"] = "FINALIZING"
                trades.sort(key=lambda trade: trade.signal_time_ms)
                summary = summarize(
                    days=days,
                    coins_selected=len(symbols),
                    coins_tested=int(state["tested"]),
                    data_errors=int(state["data_errors"]),
                    trades=trades,
                    diagnostics=diagnostics,
                )
                LOGGER.info(
                    "BACKTEST COMPLETE | days=%d tested=%d data_errors=%d engine_errors=%d simulation_errors=%d signals=%d duration=%.2fs",
                    days, state["tested"], state["data_errors"], state["engine_errors"], state["simulation_errors"], len(trades), time.monotonic() - started,
                )
                return summary
            finally:
                stop_event.set()
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)