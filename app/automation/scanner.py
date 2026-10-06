from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Any

from ..analysis.engine import (
    APPROVED_TIMEFRAMES,
    analyze_candles,
    btc_filter_ok,
    build_btc_context,
    closed_candle_rows,
    synthesize_12h_from_4h,
)
from ..config import Settings
from .executor import MexcExecutor
from .mexc_client import MexcAPIError, MexcClient
from .signal_manager import SignalManager
from .signal_validator import validate_signal
from .universe import MexcUniverse

LOGGER = logging.getLogger(__name__)

MEXC_INTERVALS = {"1D": "Day1", "12H": None, "4H": "Hour4", "1H": "Min60"}
TIMEFRAME_MS = {"1D": 86_400_000, "12H": 43_200_000, "4H": 14_400_000, "1H": 3_600_000}
CANDLE_LIMITS = {"1D": 220, "12H": 220, "4H": 650, "1H": 250}


class MexcScanner:
    """Closed-candle MEXC Futures scanner using only 1D/12H/4H/1H."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: MexcClient,
        universe: MexcUniverse,
        signal_manager: SignalManager,
        executor: MexcExecutor | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.universe = universe
        self.signal_manager = signal_manager
        self.executor = executor
        self._btc_context: dict[str, Any] = {"ok": False, "reason": "not loaded"}
        self._candle_cache: dict[tuple[str, str], tuple[int, list[Any]]] = {}
        self._candle_fetch_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._cache_limit = max(180, int(getattr(settings, "candle_limit", 650)))

    async def scan_once(self) -> dict[str, int]:
        symbols = await self.universe.refresh()
        stats = self._empty_stats(len(symbols))
        if not symbols:
            LOGGER.warning("MEXC scanner: empty universe")
            self._log_stats(stats)
            return stats

        await self._refresh_btc_context()
        configured = int(getattr(self.settings, "scan_concurrency", 4))
        concurrency = max(1, min(4, configured))
        queue: asyncio.Queue[str] = asyncio.Queue()
        for symbol in symbols:
            queue.put_nowait(symbol)
        results: list[dict[str, Any]] = []
        completed = 0

        async def worker(worker_id: int) -> None:
            nonlocal completed
            while True:
                try:
                    symbol = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    results.append(await self._scan_one(symbol))
                except Exception as exc:
                    LOGGER.exception("Scanner worker %s crashed on %s", worker_id, symbol)
                    results.append(self._error(symbol, f"worker exception: {exc}"))
                finally:
                    completed += 1
                    queue.task_done()
                    if completed == 1 or completed % 25 == 0 or completed == len(symbols):
                        LOGGER.info("MEXC scan progress: %d/%d", completed, len(symbols))

        workers = [asyncio.create_task(worker(i), name=f"mexc-scan-worker-{i}") for i in range(concurrency)]
        await queue.join()
        await asyncio.gather(*workers, return_exceptions=True)

        for result in results:
            self._merge_result(stats, result)
        self._log_stats(stats)
        return stats

    def _empty_stats(self, symbols: int) -> dict[str, int]:
        return {
            "symbols": symbols,
            "data_valid": 0,
            "pass_1d": 0,
            "pass_12h": 0,
            "pass_4h": 0,
            "pass_1h": 0,
            "regime_pass": 0,
            "bias_pass": 0,
            "setup_pass": 0,
            "trigger_pass": 0,
            "quality_pass": 0,
            "risk_pass": 0,
            "rr_pass": 0,
            "final_pass": 0,
            "valid": 0,
            "sent": 0,
            "errors": 0,
            "rejected": 0,
            "rejected_btc": 0,
            "rejected_execution": 0,
            "rejected_final": 0,
            "duplicate": 0,
            "cache_hits": 0,
            "reason_counts": Counter(),
        }

    def _merge_result(self, stats: dict[str, int], result: dict[str, Any]) -> None:
        for key in stats:
            if key in {"reason_counts"}:
                continue
            if key in result and isinstance(result[key], (int, bool)):
                stats[key] += int(result[key])
        if result.get("rejection_stage"):
            stats["rejected"] += 1
        if result.get("error"):
            stats["errors"] += 1
        for reason in result.get("rejection_reasons") or []:
            stats["reason_counts"][str(reason)] += 1

    def _log_stats(self, stats: dict[str, Any]) -> None:
        LOGGER.info(
            "MEXC GATE REPORT | symbols=%d data=%d 1D=%d 12H=%d 4H=%d 1H=%d "
            "regime=%d bias=%d setup=%d trigger=%d quality=%d risk=%d rr=%d final=%d "
            "sent=%d rejected=%d btc=%d execution=%d duplicate=%d errors=%d cache_hits=%d",
            stats["symbols"], stats["data_valid"], stats["pass_1d"], stats["pass_12h"],
            stats["pass_4h"], stats["pass_1h"], stats["regime_pass"], stats["bias_pass"],
            stats["setup_pass"], stats["trigger_pass"], stats["quality_pass"], stats["risk_pass"],
            stats["rr_pass"], stats["final_pass"], stats["sent"], stats["rejected"],
            stats["rejected_btc"], stats["rejected_execution"], stats["duplicate"], stats["errors"],
            stats["cache_hits"],
        )
        reasons = stats.get("reason_counts") or {}
        if reasons:
            top = reasons.most_common(8) if hasattr(reasons, "most_common") else sorted(reasons.items(), key=lambda x: (-x[1], x[0]))[:8]
            LOGGER.info("MEXC REJECTION REASONS | %s", " | ".join(f"{reason}={count}" for reason, count in top))

    async def _get_closed_candles(self, symbol: str, timeframe: str, limit: int) -> tuple[list[Any], bool]:
        tf = timeframe.upper()
        if tf not in APPROVED_TIMEFRAMES:
            raise ValueError(f"Unsupported analysis timeframe: {timeframe}")
        key = (symbol.upper(), tf)
        now_ms = int(time.time() * 1000)
        expected_open = (now_ms // TIMEFRAME_MS[tf]) * TIMEFRAME_MS[tf] - TIMEFRAME_MS[tf]
        cached = self._candle_cache.get(key)
        minimum_cached = min(int(limit), 180)
        if cached and cached[0] >= expected_open and len(cached[1]) >= minimum_cached:
            return cached[1], True
        lock = self._candle_fetch_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._candle_cache.get(key)
            if cached and cached[0] >= expected_open and len(cached[1]) >= minimum_cached:
                return cached[1], True
            if tf == "12H":
                raw = await self.client.get_klines(symbol, "Hour4", min(2000, int(limit) * 3 + 6))
                rows = synthesize_12h_from_4h(closed_candle_rows(raw, "4H", now_ms), now_ms=now_ms)
            else:
                raw = await self.client.get_klines(symbol, MEXC_INTERVALS[tf], limit)
                rows = closed_candle_rows(raw, tf, now_ms)
            rows = rows[-int(limit):]
            if rows:
                self._candle_cache[key] = (int(rows[-1]["time"]), rows)
            return rows, False

    async def _refresh_btc_context(self) -> None:
        try:
            c1d, _ = await self._get_closed_candles("BTC_USDT", "1D", 220)
            c4, _ = await self._get_closed_candles("BTC_USDT", "4H", 650)
            c1, _ = await self._get_closed_candles("BTC_USDT", "1H", 250)
            c12 = synthesize_12h_from_4h(c4)
            if len(c1d) < 120 or len(c12) < 60 or len(c4) < 180 or len(c1) < 180:
                self._btc_context = {"ok": False, "reason": "insufficient BTC history"}
                return
            self._btc_context = build_btc_context(c1d, c12, c4, c1)
            LOGGER.info("BTC context refreshed: %s", self._btc_context)
        except Exception as exc:
            LOGGER.warning("BTC market context unavailable; filter will abstain: %s", exc)
            self._btc_context = {"ok": False, "reason": str(exc)}

    async def _scan_one(self, symbol: str) -> dict[str, Any]:
        cache_hits = 0
        try:
            c4, hit4 = await self._get_closed_candles(symbol, "4H", 650)
            c1, hit1 = await self._get_closed_candles(symbol, "1H", 250)
            c1d, hitd = await self._get_closed_candles(symbol, "1D", 220)
            cache_hits += int(hit4) + int(hit1) + int(hitd)
            c12 = synthesize_12h_from_4h(c4)

            mins = {"1D": 120, "12H": 60, "4H": 180, "1H": 180}
            payload: dict[str, Any] = {"candle_cache_hits": cache_hits}
            for tf, rows in (("1D", c1d), ("12H", c12), ("4H", c4), ("1H", c1)):
                if len(rows) < mins[tf]:
                    return self._reject(symbol, f"Insufficient closed {tf} candles ({len(rows)}<{mins[tf]})", "DATA", payload)
                payload[f"pass_{tf.lower()}"] = 1
            payload["data_valid"] = 1

            analysis = analyze_candles(symbol, c1d, c12, c4, c1, btc_context=self._btc_context)
            payload["analysis"] = analysis
            stages = analysis.get("stage_status") or {}
            payload["regime_pass"] = int(bool(stages.get("1D_REGIME")))
            payload["bias_pass"] = int(bool(stages.get("12H_BIAS")))
            payload["setup_pass"] = int(bool(analysis.get("structure_ok") and analysis.get("setup_ok")))
            payload["trigger_pass"] = int(bool(stages.get("1H_TRIGGER")))
            payload["quality_pass"] = int(bool(stages.get("QUALITY", float(analysis.get("score", 0) or 0) >= int(getattr(self.settings, "min_confluence", 65)))))
            payload["risk_pass"] = int(bool(stages.get("RISK", analysis.get("risk_ok", False))))
            payload["rr_pass"] = int(bool(stages.get("RR", float(analysis.get("rr", 0) or 0) >= float(getattr(self.settings, "min_rr", 2.0)))))

            side = str(analysis.get("setup") or "").upper()
            failures = analysis.get("diagnostic_failures") or ["No actionable setup"]
            if side not in {"LONG", "SHORT"}:
                stage = str(analysis.get("rejection_stage") or "SETUP")
                return self._reject(symbol, failures, stage, payload)

            # Check all strategy stages for diagnostics; reject at the first failed gate.
            # Reject at the first failed strategy gate while retaining all stage counters.
            ordered = [
                ("DIRECTION", bool(analysis.get("direction_ok")), "1D/12H/4H direction"),
                ("4H_SETUP", bool(analysis.get("structure_ok") and analysis.get("setup_ok")), "4H BOS/retest setup"),
                ("1H_TRIGGER", bool(analysis.get("confirmation_ok")), "1H execution trigger"),
                ("QUALITY", bool(stages.get("QUALITY", float(analysis.get("score", 0) or 0) >= int(getattr(self.settings, "min_confluence", 65)))), f"Quality score {analysis.get('score', 0)} < {int(getattr(self.settings, "min_confluence", 65))}"),
                ("RISK", bool(stages.get("RISK", analysis.get("risk_ok", False))), "Structural risk model"),
                ("RR", bool(stages.get("RR", float(analysis.get("rr", 0) or 0) >= float(getattr(self.settings, "min_rr", 2.0)))), f"Post-cost RR {float(analysis.get('rr', 0) or 0):.2f} < {float(getattr(self.settings, "min_rr", 2.0)):.2f}"),
            ]
            for stage, passed, fallback in ordered:
                if not passed:
                    reasons = (analysis.get("stage_failures") or {}).get(stage) or [fallback]
                    return self._reject(symbol, reasons, stage, payload)

            # BTC regime is observational at setup-generation time. The portfolio
            # layer can use it for sizing/concurrency without suppressing valid
            # structural coin setups here.
            btc_ok, btc_reason = btc_filter_ok(side, self._btc_context, is_btc=symbol.upper().startswith("BTC"))
            payload["btc_would_block"] = int(not btc_ok)
            payload["btc_filter_reason"] = btc_reason

            try:
                ticker = await self.client.get_ticker(symbol)
            except Exception as exc:
                return self._reject(symbol, f"Ticker unavailable: {exc}", "EXECUTION", payload)

            max_spread_config = float(getattr(self.settings, "max_mexc_spread_pct", 0.001))
            # Configuration is historically stored as a fraction (0.001 = 0.1%).
            max_spread_pct = max_spread_config * 100.0 if 0.0 < max_spread_config <= 1.0 else max_spread_config
            quote_ok, quote_reason, spread_pct = self._ticker_quality(
                ticker,
                float(getattr(self.settings, "max_data_age_seconds", 15.0)),
                max_spread_pct=max_spread_pct,
            )
            analysis["mexc_spread_pct"] = spread_pct
            analysis["max_allowed_spread_pct"] = max_spread_pct
            if not quote_ok:
                payload["rejected_execution"] = 1
                return self._reject(symbol, quote_reason, "EXECUTION", payload)

            # MEXC kline timestamps are candle-open timestamps. Decision time is the
            # close of the completed 1H candle; use that consistently for freshness
            # validation and signal identity.
            candle_close_time = int(analysis.get("candle_close_time") or (int(c1[-1]["time"]) + TIMEFRAME_MS["1H"]))
            analysis["candle_time"] = candle_close_time
            analysis["max_signal_age_seconds"] = int(getattr(self.settings, "max_signal_age_seconds", 5400))

            signal, reasons = validate_signal(
                analysis,
                min_confluence=int(getattr(self.settings, "min_confluence", 65)),
                min_rr=float(getattr(self.settings, "min_rr", 2.0)),
                require_increasing_volume=bool(getattr(self.settings, "require_increasing_volume", False)),
            )
            if signal is None:
                payload["rejected_final"] = 1
                return self._reject(symbol, reasons, "FINAL", payload)

            payload["final_pass"] = 1
            try:
                sent = await self.signal_manager.publish(signal)
            except Exception as exc:
                LOGGER.exception("Signal dispatch failed for %s", symbol)
                return self._error(symbol, f"dispatch exception: {exc}", payload)

            if sent and self.executor is not None and getattr(self.settings, "auto_trade_enabled", False) and getattr(self.settings, "allow_live_execution", False):
                try:
                    await self._execute_signal(signal)
                except Exception:
                    LOGGER.exception("Live execution failed for %s", symbol)
            return {
                "symbol": symbol,
                "valid": True,
                "sent": bool(sent),
                "final_pass": 1,
                "analysis": analysis,
                "candle_cache_hits": cache_hits,
                "duplicate": 0,
            }
        except MexcAPIError as exc:
            LOGGER.warning(
                "MEXC symbol data error | symbol=%s code=%s status=%s reason=%s",
                symbol, exc.code, exc.status_code, exc,
            )
            return self._error(
                symbol,
                f"MEXC_API_ERROR code={exc.code or 'UNKNOWN'} status={exc.status_code or '-'}: {exc}",
                {"candle_cache_hits": cache_hits},
            )
        except Exception as exc:
            LOGGER.exception("MEXC symbol scan failed: %s", symbol)
            return self._error(
                symbol,
                f"CALCULATION_ERROR {type(exc).__name__}: {exc}",
                {"candle_cache_hits": cache_hits},
            )

    @staticmethod
    def _ticker_quality(
        ticker: dict[str, Any],
        max_age_seconds: float = 15.0,
        *,
        max_spread_pct: float = 0.50,
    ) -> tuple[bool, str, float]:
        def number(key: str) -> float | None:
            try:
                value = float(ticker.get(key))
                return value if value > 0 else None
            except (TypeError, ValueError):
                return None
        last = number("lastPrice") or number("last") or number("fairPrice")
        if last is None:
            return False, "Ticker has no positive last price", 0.0
        bid = number("bid1") or number("bidPrice")
        ask = number("ask1") or number("askPrice")
        if bid is not None and ask is not None and ask >= bid > 0:
            spread_pct = (ask - bid) / last * 100.0
            if spread_pct > float(max_spread_pct):
                return (
                    False,
                    f"Spread {spread_pct:.3f}% exceeds {float(max_spread_pct):.3f}%",
                    spread_pct,
                )
        else:
            spread_pct = 0.0
        raw_ts = ticker.get("timestamp")
        if raw_ts is not None:
            try:
                ts = int(float(raw_ts))
                ts = ts if ts >= 10**12 else ts * 1000
                age = abs(int(time.time() * 1000) - ts) / 1000.0
                if age > float(max_age_seconds):
                    return False, f"Ticker timestamp is stale by {age:.1f}s", spread_pct
            except (TypeError, ValueError):
                return False, "Ticker timestamp is malformed", spread_pct
        return True, "OK", spread_pct

    async def _execute_signal(self, signal):
        if self.executor is None:
            return None
        meta = self.universe.get(signal.symbol) if hasattr(self.universe, "get") else None
        if meta is None:
            return None
        return await self.executor.execute(signal, meta)

    @staticmethod
    def _reject(symbol: str, reason: Any, stage: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(extra or {})
        payload["rejection_stage"] = stage
        payload["rejection_reasons"] = reason if isinstance(reason, list) else [str(reason)]
        analysis = payload.get("analysis") or {}
        LOGGER.debug("MEXC REJECT | %s | stage=%s | reason=%s", symbol, stage, payload["rejection_reasons"])
        return {
            "symbol": symbol,
            "valid": False,
            "sent": False,
            "error": False,
            "rejection_stage": stage,
            "rejection_reasons": payload["rejection_reasons"],
            "analysis": analysis,
            **{k: v for k, v in payload.items() if k != "analysis"},
        }

    @staticmethod
    def _error(symbol: str, reason: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"symbol": symbol, "valid": False, "sent": False, "error": True, "rejection_stage": "ERROR", "rejection_reasons": [reason], **(extra or {})}
