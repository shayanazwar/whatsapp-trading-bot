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
from .paper_trader import PaperTrader
from .signal_validator import validate_signal
from .universe import MexcUniverse

LOGGER = logging.getLogger(__name__)

MEXC_INTERVALS = {"1D": "Day1", "12H": None, "4H": "Hour4", "1H": "Min60"}
TIMEFRAME_MS = {"1D": 86_400_000, "12H": 43_200_000, "4H": 14_400_000, "1H": 3_600_000}
MAX_LIVE_SIGNAL_AGE_SECONDS = 300.0  # V11 next-1H-open execution grace window
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
        paper_trader: PaperTrader | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.universe = universe
        self.signal_manager = signal_manager
        self.executor = executor
        self.paper_trader = paper_trader
        self._btc_context: dict[str, Any] = {"ok": False, "reason": "not loaded"}
        self._candle_cache: dict[tuple[str, str], tuple[int, list[Any]]] = {}
        self._candle_fetch_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._cache_limit = max(180, int(getattr(settings, "candle_limit", 650)))

    @staticmethod
    def _reprice_for_next_1h_open(
        analysis: dict[str, Any],
        futures_context: dict[str, Any],
        *,
        candle_close_time_ms: int,
        now_ms: int,
        max_age_seconds: float,
        max_open_gap_pct: float,
        min_rr: float,
    ) -> tuple[bool, str]:
        """Move a technical V11 signal from signal-close reference to next-open execution.

        V11 analysis is decided on the completed 1H close. Live execution is a
        MARKET order at the next 1H open, represented here by the executable
        top-of-book quote observed immediately after the boundary. A stale scan
        is rejected rather than executing an old close as if it were the new open.
        """
        age_seconds = max(0.0, (int(now_ms) - int(candle_close_time_ms)) / 1000.0)
        hard_max_age = min(max(float(max_age_seconds), 0.0), MAX_LIVE_SIGNAL_AGE_SECONDS)
        if age_seconds > hard_max_age:
            return False, f"1H setup age {age_seconds:.1f}s exceeds live next-open window {hard_max_age:.1f}s"
        if int(now_ms) < int(candle_close_time_ms):
            return False, "Next 1H open has not occurred yet"

        side = str(analysis.get("setup") or "").upper()
        reference_entry = float(analysis.get("entry") or 0.0)
        if reference_entry <= 0 or side not in {"LONG", "SHORT"}:
            return False, "Invalid signal-close entry reference"
        executable_entry = futures_context.get("best_ask" if side == "LONG" else "best_bid")
        if executable_entry is None:
            executable_entry = futures_context.get("last_price")
        try:
            executable_entry = float(executable_entry)
        except (TypeError, ValueError):
            return False, "Executable next-open quote is unavailable"
        if executable_entry <= 0:
            return False, "Executable next-open quote is invalid"

        gap_pct = abs(executable_entry - reference_entry) / reference_entry * 100.0
        configured = max(0.0, float(max_open_gap_pct))
        if configured <= 1.0:
            configured *= 100.0
        if gap_pct > configured:
            return False, f"Next-open gap {gap_pct:.3f}% exceeds {configured:.3f}%"

        stop = float(analysis.get("stop_loss") or 0.0)
        target = float(analysis.get("tp") or 0.0)
        if side == "LONG":
            geometry_ok = stop < executable_entry < target
        else:
            geometry_ok = target < executable_entry < stop
        if not geometry_ok:
            return False, "Next-open market price breaks structural SL/TP geometry"

        risk = abs(executable_entry - stop)
        reward = abs(target - executable_entry)
        if risk <= 0:
            return False, "Next-open structural risk is zero"
        signal_cost = max(0.0, float(analysis.get("estimated_round_trip_cost_pct", 0.0015) or 0.0015))
        cost_price = executable_entry * signal_cost
        rr_gross = reward / risk
        rr_net = (reward - cost_price) / (risk + cost_price) if risk + cost_price > 0 else 0.0
        if rr_net + 1e-12 < float(min_rr):
            return False, f"Next-open post-cost RR {rr_net:.2f} < {float(min_rr):.2f}"

        atr4 = float(analysis.get("atr_4h") or analysis.get("atr") or 0.0)
        atr1 = float(analysis.get("atr_1h") or analysis.get("atr") or 0.0)
        sl_atr4 = risk / atr4 if atr4 > 0 else 0.0
        sl_atr1 = risk / atr1 if atr1 > 0 else 0.0
        analysis.update({
            "signal_close_entry": reference_entry,
            "entry": executable_entry,
            "entry_time": int(candle_close_time_ms),
            "entry_reference_time": int(candle_close_time_ms),
            "entry_reference_price": executable_entry,
            "next_1h_open_reference": executable_entry,
            "opening_gap_pct": gap_pct,
            "entry_drift_pct": gap_pct,
            "rr_gross": rr_gross,
            "rr": rr_net,
            "rr_net": rr_net,
            "sl_atr": sl_atr4,
            "sl_atr_4h": sl_atr4,
            "sl_atr_1h": sl_atr1,
            "stop_distance_pct": risk / executable_entry,
            "tp_distance_atr": reward / atr4 if atr4 > 0 else 0.0,
            "tp_distance_atr_1h": reward / atr1 if atr1 > 0 else 0.0,
            "tp_distance_pct": reward / executable_entry,
            "trade_geometry_ok": True,
            "risk_ok": True,
            "rr_ok": rr_net >= float(min_rr),
            "entry_distance_ok": True,
        })
        return True, "OK"

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
            if len(c1d) < 210 or len(c12) < 60 or len(c4) < 180 or len(c1) < 180:
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

            mins = {"1D": 210, "12H": 60, "4H": 180, "1H": 180}
            payload: dict[str, Any] = {"candle_cache_hits": cache_hits}
            for tf, rows in (("1D", c1d), ("12H", c12), ("4H", c4), ("1H", c1)):
                if len(rows) < mins[tf]:
                    return self._reject(symbol, f"Insufficient closed {tf} candles ({len(rows)}<{mins[tf]})", "DATA", payload)
                payload[f"pass_{tf.lower()}"] = 1
            payload["data_valid"] = 1

            analysis = analyze_candles(
                symbol, c1d, c12, c4, c1,
                btc_context=self._btc_context,
                estimated_round_trip_cost_pct=float(getattr(self.settings, "estimated_round_trip_cost_pct", 0.0015)),
            )
            payload["analysis"] = analysis
            stages = analysis.get("stage_status") or {}
            payload["regime_pass"] = int(bool(stages.get("1D_REGIME")))
            payload["bias_pass"] = int(bool(stages.get("12H_BIAS")))
            payload["setup_pass"] = int(bool(analysis.get("structure_ok") and analysis.get("setup_ok")))
            payload["trigger_pass"] = int(bool(stages.get("1H_TRIGGER")))
            payload["quality_pass"] = int(bool(stages.get("QUALITY", float(analysis.get("score", 0) or 0) >= int(getattr(self.settings, "min_confluence", 65)))))
            payload["risk_pass"] = int(bool(stages.get("RISK", analysis.get("risk_ok", False))))
            payload["rr_pass"] = int(bool(stages.get("RR", float(analysis.get("rr", 0) or 0) >= float(getattr(self.settings, "min_rr", 1.6)))))

            side = str(analysis.get("setup") or "").upper()
            failures = analysis.get("diagnostic_failures") or ["No actionable setup"]
            if side not in {"LONG", "SHORT"}:
                stage = str(analysis.get("rejection_stage") or "SETUP")
                return self._reject(symbol, failures, stage, payload)

            # Keep SHORT analysis available for diagnostics, but optionally block
            # SHORT signal/trade creation at the scanner boundary.
            if side == "SHORT" and not bool(getattr(self.settings, "shorts_enabled", True)):
                payload["short_analysis_detected"] = 1
                return self._reject(symbol, "SHORT signals disabled by SHORTS_ENABLED=false", "SIDE_DISABLED", payload)

            # Check all strategy stages for diagnostics; reject at the first failed gate.
            # Reject at the first failed strategy gate while retaining all stage counters.
            ordered = [
                ("DIRECTION", bool(analysis.get("direction_ok")), "1D/12H/4H direction"),
                ("4H_SETUP", bool(analysis.get("structure_ok") and analysis.get("setup_ok")), "4H impulse/value setup"),
                ("1H_TRIGGER", bool(analysis.get("confirmation_ok")), "1H liquidity sweep/reclaim"),
                ("VOLATILITY", bool(analysis.get("volatility_ok")), "volatility sanity"),
                ("CONFIRMATION_FAMILIES", bool(analysis.get("confirmation_family_diversity_ok")), "supporting evidence"),
                ("TARGET_PATH", bool(analysis.get("location_ok") and analysis.get("target_path_structural") and analysis.get("target_path_clear")), "Clear HTF target path"),
                ("RISK", bool(stages.get("RISK", analysis.get("risk_ok", False))), "Structural risk model"),
                ("RR", bool(stages.get("RR", float(analysis.get("rr", 0) or 0) >= float(getattr(self.settings, "min_rr", 1.6)))), f"Post-cost RR {float(analysis.get('rr', 0) or 0):.2f} < {float(getattr(self.settings, "min_rr", 1.6)):.2f}"),
                ("BTC", bool(analysis.get("btc_filter_ok")), str(analysis.get("btc_filter_reason") or "BTC filter")),
                ("QUALITY", bool(stages.get("QUALITY", float(analysis.get("score", 0) or 0) >= int(getattr(self.settings, "min_confluence", 65)))), f"V11 score gate"),
                ("TECHNICAL_CANDIDATE", bool(analysis.get("technical_candidate")), "Engine technical candidate gate"),
            ]
            for stage, passed, fallback in ordered:
                if not passed:
                    reasons = (analysis.get("stage_failures") or {}).get(stage) or [fallback]
                    return self._reject(symbol, reasons, stage, payload)

            btc_ok, btc_reason = btc_filter_ok(side, self._btc_context, is_btc=symbol.upper().startswith("BTC"))
            payload["btc_would_block"] = int(not btc_ok)
            payload["btc_filter_reason"] = btc_reason
            if not btc_ok:
                return self._reject(symbol, btc_reason, "BTC", payload)

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

            futures_context = await self._get_futures_execution_context(
                symbol, ticker, spread_pct=spread_pct
            )
            analysis["futures_context"] = futures_context
            analysis["mexc_funding_rate"] = futures_context.get("funding_rate")
            analysis["futures_ok"] = bool(futures_context.get("execution_ok"))
            analysis["futures_execution_ok"] = bool(futures_context.get("execution_ok"))
            analysis["futures_context_checked"] = True
            analysis["data_fresh"] = bool(quote_ok)
            if not futures_context.get("execution_ok"):
                payload["rejected_execution"] = 1
                return self._reject(
                    symbol,
                    futures_context.get("errors") or ["Futures execution quality check failed"],
                    "EXECUTION",
                    payload,
                )

            # MEXC kline timestamps are candle-open timestamps. V11 decision time is
            # the CLOSE of the completed 1H candle. The next 1H open is the only live
            # entry point; stale discoveries are rejected rather than back-filled.
            candle_close_time = int(analysis.get("candle_close_time") or (int(c1[-1]["time"]) + TIMEFRAME_MS["1H"]))
            analysis["candle_time"] = candle_close_time
            configured_age = float(getattr(self.settings, "max_signal_age_seconds", MAX_LIVE_SIGNAL_AGE_SECONDS) or MAX_LIVE_SIGNAL_AGE_SECONDS)
            analysis["max_signal_age_seconds"] = min(configured_age, MAX_LIVE_SIGNAL_AGE_SECONDS)
            ok_open, open_reason = self._reprice_for_next_1h_open(
                analysis,
                futures_context,
                candle_close_time_ms=candle_close_time,
                now_ms=int(time.time() * 1000),
                max_age_seconds=configured_age,
                max_open_gap_pct=float(getattr(self.settings, "max_entry_drift_pct", 0.002)),
                min_rr=float(getattr(self.settings, "min_rr", 1.6)),
            )
            if not ok_open:
                payload["rejected_execution"] = 1
                return self._reject(symbol, open_reason, "EXECUTION", payload)

            signal, reasons = validate_signal(
                analysis,
                min_confluence=int(getattr(self.settings, "min_confluence", 65)),
                min_rr=float(getattr(self.settings, "min_rr", 1.6)),
                require_increasing_volume=bool(getattr(self.settings, "require_increasing_volume", False)),
            )
            if signal is None:
                payload["rejected_final"] = 1
                return self._reject(symbol, reasons, "FINAL", payload)

            payload["final_pass"] = 1

            # Paper trading consumes the same fully validated V11 signal, but
            # is independent from outbound message delivery and live execution.
            if self.paper_trader is not None and getattr(self.settings, "paper_trading_enabled", False):
                try:
                    self.paper_trader.open_from_signal(signal)
                except Exception:
                    LOGGER.exception("Paper trade opening failed for %s", symbol)

            sent = False
            if getattr(self.settings, "auto_signal_enabled", False):
                try:
                    sent = await self.signal_manager.publish(signal)
                except Exception as exc:
                    LOGGER.exception("Signal dispatch failed for %s", symbol)
                    return self._error(symbol, f"dispatch exception: {exc}", payload)

            # Live execution is explicitly suppressed whenever the virtual-money
            # mode is enabled, even if environment flags are accidentally mixed.
            if (sent and not getattr(self.settings, "paper_trading_enabled", False)
                    and self.executor is not None
                    and getattr(self.settings, "auto_trade_enabled", False)
                    and getattr(self.settings, "allow_live_execution", False)):
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

    @staticmethod
    def _number_from_payload(
        payload: Any,
        *keys: str,
        positive_only: bool = False,
    ) -> float | None:
        if isinstance(payload, dict):
            for key in keys:
                try:
                    value = float(payload.get(key))
                except (TypeError, ValueError):
                    continue
                if value == value and value not in (float("inf"), -float("inf")):
                    if positive_only and value <= 0:
                        continue
                    return value
        return None

    @classmethod
    def _price_from_payload(cls, payload: Any, *keys: str) -> float | None:
        return cls._number_from_payload(payload, *keys, positive_only=True)

    @staticmethod
    def _book_levels(payload: Any, side: str, limit: int) -> list[tuple[float, float]]:
        if not isinstance(payload, dict):
            return []
        raw = payload.get(side) or payload.get(side.lower()) or []
        levels: list[tuple[float, float]] = []
        for item in list(raw)[:max(1, int(limit))]:
            try:
                if isinstance(item, dict):
                    price = float(item.get("price", item.get("p")))
                    qty = float(item.get("vol", item.get("quantity", item.get("qty", item.get("v")))))
                else:
                    price = float(item[0])
                    qty = float(item[1])
                if price > 0 and qty > 0:
                    levels.append((price, qty))
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        return levels

    async def _get_futures_execution_context(
        self,
        symbol: str,
        ticker: dict[str, Any],
        *,
        spread_pct: float,
    ) -> dict[str, Any]:
        """Build truthful live futures execution context after technical gates."""
        tasks = await asyncio.gather(
            self.client.get_depth(symbol, int(getattr(self.settings, "orderbook_levels", 10))),
            self.client.get_index_price(symbol),
            self.client.get_fair_price(symbol),
            self.client.get_funding_rate(symbol),
            return_exceptions=True,
        )
        depth, index_payload, fair_payload, funding_payload = tasks

        context: dict[str, Any] = {
            "status": "PARTIAL",
            "execution_ok": False,
            "spread_pct": float(spread_pct),
            "ticker_timestamp_ms": None,
            "ticker_age_seconds": None,
            "last_price": self._price_from_payload(ticker, "lastPrice", "last", "fairPrice"),
            "best_bid": None,
            "best_ask": None,
            "top_bid_qty": 0.0,
            "top_ask_qty": 0.0,
            "top_bid_notional": 0.0,
            "top_ask_notional": 0.0,
            "orderbook_levels": int(getattr(self.settings, "orderbook_levels", 10)),
            "orderbook_checked": False,
            "index_price": self._price_from_payload(index_payload, "indexPrice", "index_price", "price") if not isinstance(index_payload, Exception) else None,
            "fair_price": self._price_from_payload(fair_payload, "fairPrice", "fair_price", "price") if not isinstance(fair_payload, Exception) else None,
            "funding_rate": self._number_from_payload(funding_payload, "fundingRate", "funding_rate", "funding") if not isinstance(funding_payload, Exception) else None,
            "index_dislocation_pct": None,
            "fair_dislocation_pct": None,
            "index_dislocation_checked": False,
            "funding_checked": not isinstance(funding_payload, Exception),
            "errors": [],
        }

        for label, payload_value in (("depth", depth), ("index", index_payload), ("fair", fair_payload), ("funding", funding_payload)):
            if isinstance(payload_value, Exception):
                context["errors"].append(f"{label}: {payload_value}")

        raw_ts = ticker.get("timestamp")
        if raw_ts is not None:
            try:
                ts = int(float(raw_ts))
                ts = ts if ts >= 10**12 else ts * 1000
                age = max(0.0, (int(time.time() * 1000) - ts) / 1000.0)
                context["ticker_timestamp_ms"] = ts
                context["ticker_age_seconds"] = age
            except (TypeError, ValueError):
                context["errors"].append("ticker timestamp malformed")
        else:
            context["errors"].append("ticker timestamp missing")

        bids = self._book_levels(depth, "bids", context["orderbook_levels"]) if not isinstance(depth, Exception) else []
        asks = self._book_levels(depth, "asks", context["orderbook_levels"]) if not isinstance(depth, Exception) else []
        if bids and asks:
            context["orderbook_checked"] = True
            context["best_bid"] = bids[0][0]
            context["best_ask"] = asks[0][0]
            context["top_bid_qty"] = sum(q for _, q in bids)
            context["top_ask_qty"] = sum(q for _, q in asks)
            context["top_bid_notional"] = sum(p * q for p, q in bids)
            context["top_ask_notional"] = sum(p * q for p, q in asks)
            if context["last_price"] and context["spread_pct"] <= 0:
                context["spread_pct"] = (context["best_ask"] - context["best_bid"]) / context["last_price"] * 100.0
        else:
            context["errors"].append("order book bids/asks unavailable")

        last = context["last_price"]
        index_price = context["index_price"]
        fair_price = context["fair_price"]
        max_dislocation_cfg = float(getattr(self.settings, "max_index_dislocation_pct", 0.002))
        max_dislocation_pct = max_dislocation_cfg * 100.0 if 0.0 < max_dislocation_cfg <= 1.0 else max_dislocation_cfg
        if last and index_price:
            context["index_dislocation_pct"] = abs(last - index_price) / index_price * 100.0
            context["index_dislocation_checked"] = True
        if last and fair_price:
            context["fair_dislocation_pct"] = abs(last - fair_price) / fair_price * 100.0
            context["fair_dislocation_checked"] = True

        quote_ok = context["ticker_timestamp_ms"] is not None and context["ticker_age_seconds"] is not None and context["ticker_age_seconds"] <= float(getattr(self.settings, "max_data_age_seconds", 5.0))
        book_ok = bool(context["orderbook_checked"])
        configured_spread = float(getattr(self.settings, "max_mexc_spread_pct", 0.001))
        max_spread_pct = configured_spread * 100.0 if 0.0 < configured_spread <= 1.0 else configured_spread
        spread_known_bad = context.get("spread_pct", 0.0) > max_spread_pct
        index_known_bad = (
            context.get("index_dislocation_pct") is not None
            and context["index_dislocation_pct"] > max_dislocation_pct
        ) or (
            context.get("fair_dislocation_pct") is not None
            and context["fair_dislocation_pct"] > max_dislocation_pct
        )
        context["max_index_dislocation_pct"] = max_dislocation_pct
        context["index_ok"] = not index_known_bad
        if spread_known_bad:
            context["errors"].append(f"Order-book spread {context.get('spread_pct',0.0):.3f}% exceeds configured maximum")
        context["execution_ok"] = bool(quote_ok and book_ok and not index_known_bad and not spread_known_bad)
        if context["execution_ok"] and quote_ok and book_ok:
            context["status"] = "VERIFIED" if context.get("index_dislocation_checked") else "PARTIAL"
        return context

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
