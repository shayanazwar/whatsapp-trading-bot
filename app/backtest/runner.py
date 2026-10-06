from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..analysis.engine import analyze_candles
from ..automation.mexc_client import MexcClient
from ..automation.mexc_client import MexcAPIError
from ..automation.signal_validator import make_signal_key, validate_signal
from ..automation.universe import MexcUniverse
from ..config import Settings
from .report import BacktestSummary, summarize
from .simulator import SimulatedTrade, simulate_trade

LOGGER = logging.getLogger(__name__)

ONE_HOUR_MS = 3_600_000
MAX_HOLD_MINUTES = 72 * 60
WARMUP_1D = 300 * 86_400_000
WARMUP_4H = 45 * 86_400_000
WARMUP_1H = 20 * 86_400_000
MAX_BACKTEST_SYMBOLS = 200


class BacktestAnalysisTimeout(TimeoutError):
    pass


class BacktestAnalysisProcessError(RuntimeError):
    pass


class BacktestAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class SymbolHistory:
    symbol: str
    candles_1d: list
    candles_4h: list
    candles_1h: list


class BacktestRunner:
    """Causal paper backtester for the 1D/12H/4H/1H strategy."""

    def __init__(self, client: MexcClient, universe: MexcUniverse, settings: Settings, max_concurrency: int = 3) -> None:
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
        # End the historical decision window far enough in the past that the
        # configured forward holding horizon is available for trade resolution.
        # This prevents current-day backtests from turning still-open trades into
        # artificial EXPIRED results simply because the API has no future candles.
        end_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        end_ms = int(end_dt.timestamp() * 1000) - MAX_HOLD_MINUTES * 60_000
        start_ms = end_ms - int(days) * 86_400_000
        return start_ms, end_ms

    async def _fetch_history(self, symbol: str, start_ms: int, end_ms: int) -> SymbolHistory:
        async def fetch(interval: str, left: int, right: int, limit: int = 2000) -> list:
            rows = await self.client.get_klines_range(symbol, interval, left, right, limit=limit)
            return rows or []

        # Historical data are fetched with independent warmups and a forward window
        # large enough to resolve trades without ever feeding future candles into the
        # signal decision itself.
        candles_1d, candles_4h, candles_1h = await asyncio.gather(
            fetch("Day1", start_ms - WARMUP_1D, end_ms + MAX_HOLD_MINUTES * 60_000),
            fetch("Hour4", start_ms - WARMUP_4H, end_ms + MAX_HOLD_MINUTES * 60_000),
            fetch("Min60", start_ms - WARMUP_1H, end_ms + MAX_HOLD_MINUTES * 60_000),
        )
        return SymbolHistory(symbol=symbol, candles_1d=candles_1d, candles_4h=candles_4h, candles_1h=candles_1h)

    async def _fetch_btc_history(self, start_ms: int, end_ms: int) -> SymbolHistory:
        async def fetch(interval: str, left: int, right: int, limit: int = 2000) -> list:
            rows = await self.client.get_klines_range("BTC_USDT", interval, left, right, limit=limit)
            return rows or []
        c1d, c4, c1 = await asyncio.gather(
            fetch("Day1", start_ms - WARMUP_1D, end_ms + MAX_HOLD_MINUTES * 60_000),
            fetch("Hour4", start_ms - WARMUP_4H, end_ms + MAX_HOLD_MINUTES * 60_000),
            fetch("Min60", start_ms - WARMUP_1H, end_ms + MAX_HOLD_MINUTES * 60_000),
        )
        return SymbolHistory("BTC_USDT", c1d, c4, c1)

    @staticmethod
    def _prefix(rows: list, decision_close_ms: int) -> list:
        # Candles are identified by opening timestamp; decision_close is the first
        # moment at which a candle ending at that time is allowed into the strategy.
        return [row for row in rows if int(float(row[0])) + ONE_HOUR_MS <= decision_close_ms]

    def _simulate_symbol(self, history: SymbolHistory, start_ms: int, end_ms: int, btc_history: SymbolHistory | None = None) -> tuple[list[SimulatedTrade], dict[str, int], int, int, int]:
        trades: list[SimulatedTrade] = []
        diag: dict[str, int] = {}
        rejected = 0
        data_errors = 0
        execution_errors = 0
        seen_keys: set[str] = set()
        active_until_ms = 0

        def inc(key: str, count: int = 1) -> None:
            diag[key] = diag.get(key, 0) + count

        for row in history.candles_1h:
            signal_open_ms = int(float(row[0]))
            signal_close_ms = signal_open_ms + ONE_HOUR_MS
            if signal_close_ms <= start_ms or signal_close_ms > end_ms:
                continue
            if signal_close_ms <= active_until_ms:
                inc("OVERLAP_SKIPPED")
                continue

            c1d = [x for x in history.candles_1d if int(float(x[0])) + 86_400_000 <= signal_close_ms]
            c4 = [x for x in history.candles_4h if int(float(x[0])) + 14_400_000 <= signal_close_ms]
            c1 = [x for x in history.candles_1h if int(float(x[0])) + ONE_HOUR_MS <= signal_close_ms]
            try:
                btc_context = None
                if btc_history is not None:
                    btc1d = [x for x in btc_history.candles_1d if int(float(x[0])) + 86_400_000 <= signal_close_ms]
                    btc4 = [x for x in btc_history.candles_4h if int(float(x[0])) + 14_400_000 <= signal_close_ms]
                    btc1 = [x for x in btc_history.candles_1h if int(float(x[0])) + ONE_HOUR_MS <= signal_close_ms]
                    if len(btc1d) >= 210 and len(btc4) >= 180 and len(btc1) >= 180:
                        from ..analysis.engine import build_btc_context
                        btc_context = build_btc_context(btc1d, None, btc4, btc1)
                analysis = analyze_candles(history.symbol, c1d, None, c4, c1, now_ms=signal_close_ms, btc_context=btc_context)
            except ValueError as exc:
                # Analysis ValueErrors are data/analysis precondition failures (for
                # example an insufficient or gapped timeframe window).  Do not count
                # the same symbol-level data defect once per 1H decision candle.
                # The old behavior inflated one bad history into hundreds of
                # ENGINE_ERROR_ValueError entries and obscured the real failure.
                data_errors += 1
                reason = str(exc).strip() or "ValueError"
                normalized = (
                    reason.upper()
                    .replace(" ", "_")
                    .replace(":", "")
                    .replace("/", "_")
                )[:120]
                inc("ENGINE_DATA_ERRORS")
                inc("ENGINE_DATA_QUALITY_ERROR")
                inc(f"ENGINE_DATA_QUALITY_{normalized}")
                LOGGER.warning(
                    "BACKTEST DATA_QUALITY_ERROR | symbol=%s signal_close_ms=%s reason=%s",
                    history.symbol,
                    signal_close_ms,
                    reason,
                )
                # A malformed/insufficient analysis window cannot become valid
                # again for the remaining candles of this short backtest window;
                # stop this symbol here instead of repeating the same failure.
                break
            except Exception as exc:
                data_errors += 1
                inc("ENGINE_DATA_ERRORS")
                inc(f"ENGINE_ERROR_{type(exc).__name__}")
                LOGGER.exception(
                    "BACKTEST ENGINE_ERROR | symbol=%s signal_close_ms=%s type=%s reason=%s",
                    history.symbol,
                    signal_close_ms,
                    type(exc).__name__,
                    exc,
                )
                continue

            inc("CANDLES_EVALUATED")
            if not analysis.get("technical_candidate"):
                rejected += 1
                failures = analysis.get("technical_gate_failures") or ["technical_candidate"]
                for failure in failures[:5]:
                    inc("REJECT_" + str(failure).upper().replace(" ", "_"))

                # Exclusive first-fail telemetry makes gate bottlenecks measurable.
                ordered_fails = [
                    ("DIRECTION", analysis.get("direction_ok") is not True),
                    ("4H_SETUP", not (analysis.get("structure_ok") is True and analysis.get("setup_ok") is True)),
                    ("1H_CONFIRMATION", analysis.get("confirmation_ok") is not True),
                    ("ENTRY_DISTANCE", analysis.get("entry_distance_ok") is not True),
                    ("HTF_TARGET_PATH", analysis.get("location_ok") is not True),
                    ("STRUCTURAL_RISK_RR", analysis.get("risk_ok") is not True),
                    ("SHOCK", analysis.get("shock_veto_ok") is not True),
                    ("BTC_REGIME", analysis.get("btc_filter_ok") is not True),
                    ("QUALITY", int(analysis.get("score", 0) or 0) < 65),
                ]
                first_fail = next((name for name, failed in ordered_fails if failed), "TECHNICAL_CANDIDATE")
                inc("FIRST_FAIL_" + first_fail)

                # Shadow score-only rejects without allowing them into production.
                if 55 <= int(analysis.get("score", 0) or 0) < 65 and all(not failed for name, failed in ordered_fails[:-1]):
                    inc("SHADOW_SCORE_55_64")
                continue

            inc("CANDIDATES_DISCOVERED")
            analysis = dict(analysis)
            # Backtest time is historical, so bypass only the wall-clock age check.
            analysis["max_signal_age_seconds"] = 10**9
            signal, reasons = validate_signal(
                analysis,
                min_confluence=int(getattr(self.settings, "min_confluence", 65)),
                min_rr=float(getattr(self.settings, "min_rr", 2.0)),
            )
            if signal is None:
                rejected += 1
                inc("FINAL_REJECT")
                for reason in reasons[:5]:
                    inc("FINAL_REJECT_" + str(reason).upper().replace(" ", "_")[:90])
                continue

            key = signal.key
            if key in seen_keys:
                inc("DUPLICATE_STRUCTURE_SKIPPED")
                continue
            seen_keys.add(key)
            inc("FINAL_ACCEPT")

            future = [x for x in history.candles_1h if int(float(x[0])) >= signal_close_ms]
            try:
                trade = simulate_trade(
                    signal.analysis,
                    future,
                    signal_close_time_ms=signal_close_ms,
                    fee_rate=0.0004,
                    slippage_bps=2.0,
                    max_holding_minutes=MAX_HOLD_MINUTES,
                    same_bar_rule="SL_FIRST",
                )
            except Exception as exc:
                execution_errors += 1
                inc("SIMULATION_ERRORS")
                inc("SIMULATION_ERROR_" + type(exc).__name__.upper())
                continue
            if trade is None:
                inc("UNRESOLVED_NO_FUTURE_DATA")
                continue
            trades.append(trade)
            active_until_ms = max(active_until_ms, int(trade.exit_time_ms or signal_close_ms))
            inc("SIMULATION_RESOLVED" if trade.r_multiple is not None else "SIMULATION_UNRESOLVED")

        return trades, diag, data_errors, execution_errors, rejected

    async def run(self, days: int) -> BacktestSummary:
        if int(days) not in {1, 7, 30, 60, 90}:
            raise ValueError("Backtest period must be 1D, 7D, 30D, 60D, or 90D")
        async with self._run_lock:
            if self._running:
                raise BacktestAlreadyRunning("A backtest is already running")
            self._running = True
        started = time.monotonic()
        try:
            start_ms, end_ms = self._period(int(days))
            symbols = await self.universe.refresh()
            selected = symbols[: max(1, min(MAX_BACKTEST_SYMBOLS, int(getattr(self.settings, "backtest_max_symbols", MAX_BACKTEST_SYMBOLS))))]
            LOGGER.info("BACKTEST %sD START | period=%s..%s symbols=%d", days, start_ms, end_ms, len(selected))
            if not selected:
                return summarize(days=days, period_start_ms=start_ms, period_end_ms=end_ms, coins_selected=0, coins_tested=0, data_errors=0, execution_errors=0, rejected_setups=0, trades=[], diagnostics={"NO_SYMBOLS": 1})

            semaphore = asyncio.Semaphore(self.max_concurrency)
            results: list[tuple[list[SimulatedTrade], dict[str, int], int, int, int]] = []
            btc_history: SymbolHistory | None = None
            try:
                btc_history = await self._fetch_btc_history(start_ms, end_ms)
                LOGGER.info("BTC BACKTEST CONTEXT READY | candles_1d=%d candles_4h=%d candles_1h=%d", len(btc_history.candles_1d), len(btc_history.candles_4h), len(btc_history.candles_1h))
            except Exception as exc:
                diagnostics_btc_error = {"BTC_CONTEXT_FETCH_ERROR": 1}
                LOGGER.warning("BTC BACKTEST CONTEXT unavailable: %s", exc)
            else:
                diagnostics_btc_error = {}

            progress_interval = max(
                1.0,
                float(getattr(self.settings, "backtest_progress_interval_seconds", 5.0)),
            )
            symbol_timeout = max(
                10.0,
                float(getattr(self.settings, "backtest_analysis_timeout_seconds", 120.0)),
            )
            progress_started = time.monotonic()
            completed = 0
            progress_lock = asyncio.Lock()

            async def one(symbol: str) -> None:
                nonlocal completed, progress_started
                async with semaphore:
                    try:
                        history = await asyncio.wait_for(
                            self._fetch_history(symbol, start_ms, end_ms),
                            timeout=symbol_timeout,
                        )
                        result = await asyncio.wait_for(
                            asyncio.to_thread(
                                self._simulate_symbol,
                                history,
                                start_ms,
                                end_ms,
                                btc_history,
                            ),
                            timeout=symbol_timeout,
                        )
                        results.append(result)
                    except asyncio.TimeoutError:
                        LOGGER.warning(
                            "BACKTEST DATA_ERROR | symbol=%s reason=SYMBOL_TIMEOUT timeout=%.1fs",
                            symbol,
                            symbol_timeout,
                        )
                        results.append(
                            ([], {"SYMBOL_TIMEOUT": 1}, 1, 0, 0)
                        )
                    except MexcAPIError as exc:
                        LOGGER.warning(
                            "BACKTEST DATA_ERROR | symbol=%s code=%s status=%s reason=%s",
                            symbol,
                            exc.code,
                            exc.status_code,
                            exc,
                        )
                        code = str(exc.code or "MEXC_API_ERROR").upper()
                        results.append(
                            ([], {f"MEXC_API_ERROR_{code}": 1}, 1, 0, 0)
                        )
                    except Exception as exc:
                        LOGGER.exception(
                            "BACKTEST CALCULATION_ERROR | symbol=%s type=%s reason=%s",
                            symbol,
                            type(exc).__name__,
                            exc,
                        )
                        results.append(
                            ([], {f"SYMBOL_ERROR_{type(exc).__name__.upper()}": 1}, 1, 0, 0)
                        )
                    finally:
                        async with progress_lock:
                            completed += 1
                            now = time.monotonic()
                            if (
                                completed == 1
                                or completed == len(selected)
                                or now - progress_started >= progress_interval
                            ):
                                elapsed = now - started
                                LOGGER.info(
                                    "BACKTEST PROGRESS | completed=%d/%d elapsed=%.1fs",
                                    completed,
                                    len(selected),
                                    elapsed,
                                )
                                progress_started = now

            await asyncio.gather(*(one(symbol) for symbol in selected))

            trades: list[SimulatedTrade] = []
            diagnostics: dict[str, int] = dict(diagnostics_btc_error)
            data_errors = execution_errors = rejected = 0
            for symbol_trades, symbol_diag, de, ee, rj in results:
                trades.extend(symbol_trades)
                data_errors += de
                execution_errors += ee
                rejected += rj
                for key, value in symbol_diag.items():
                    diagnostics[key] = diagnostics.get(key, 0) + int(value)

            # Portfolio-level causal controls: correlated universe signals are not
            # allowed to become unlimited simultaneous directional exposure.
            max_total = max(1, int(getattr(self.settings, "backtest_max_open_positions", 4)))
            max_same = max(1, int(getattr(self.settings, "backtest_max_same_direction", 2)))
            max_risk = max(0.1, float(getattr(self.settings, "backtest_total_open_risk_r", 3.0)))
            portfolio_sorted = sorted(trades, key=lambda t: (int(t.entry_filled_time_ms or t.signal_time_ms), t.symbol, t.side))
            accepted_portfolio: list[SimulatedTrade] = []
            active_portfolio: list[SimulatedTrade] = []
            for trade in portfolio_sorted:
                et = int(trade.entry_filled_time_ms or trade.signal_time_ms)
                active_portfolio = [x for x in active_portfolio if int(x.exit_time_ms or 0) > et]
                same_dir = sum(str(x.side).upper() == str(trade.side).upper() for x in active_portfolio)
                open_risk = float(len(active_portfolio))
                if len(active_portfolio) >= max_total or same_dir >= max_same or open_risk + 1.0 > max_risk:
                    diagnostics["PORTFOLIO_SKIPPED"] = diagnostics.get("PORTFOLIO_SKIPPED", 0) + 1
                    continue
                accepted_portfolio.append(trade)
                active_portfolio.append(trade)
            trades = accepted_portfolio

            diagnostics["SYMBOLS_TESTED"] = len(results)
            diagnostics["CURRENT_UNIVERSE_SNAPSHOT_BIAS"] = 1
            diagnostics["REJECTED_SETUPS"] = rejected
            diagnostics["BACKTEST_SECONDS"] = int(time.monotonic() - started)
            summary = summarize(
                days=days,
                period_start_ms=start_ms,
                period_end_ms=end_ms,
                coins_selected=len(selected),
                coins_tested=len(results),
                data_errors=data_errors,
                execution_errors=execution_errors,
                rejected_setups=rejected,
                trades=trades,
                diagnostics=diagnostics,
            )
            LOGGER.info("BACKTEST %sD COMPLETE | signals=%d resolved=%d total_r=%+.2f errors=%d", days, summary.signals, summary.resolved, summary.total_r, summary.data_errors + summary.execution_errors)
            return summary
        finally:
            async with self._run_lock:
                self._running = False
