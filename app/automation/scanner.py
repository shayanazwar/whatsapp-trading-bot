from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..analysis.engine import (
    MAX_ENTRY_DISTANCE_ATR,
    MAX_SL_ATR,
    MIN_RR,
    MIN_SL_ATR,
    MIN_TP_ATR,
    _bos_events,
    _direction_aligned,
    _four_hour_regime,
    _one_hour_alignment,
    _select_latest_bos_with_retest,
    analyze_candles,
    btc_filter_ok,
    build_btc_context,
    closed_candle_rows,
    evaluate_confirmation_families,
)
from ..config import Settings
from .executor import MexcExecutor
from .mexc_client import MexcClient
from .risk_manager import calculate_rr_after_costs
from .signal_manager import SignalManager
from .signal_validator import validate_signal
from .universe import MexcUniverse

LOGGER = logging.getLogger(__name__)

MEXC_INTERVALS = {
    "4H": "Hour4",
    "1H": "Min60",
    "15M": "Min15",
    "1D": "Day1",
}
TIMEFRAME_MS = {
    "4H": 14_400_000,
    "1H": 3_600_000,
    "15M": 900_000,
    "1D": 86_400_000,
}
CANDLE_LIMITS = {"4H": 220, "1H": 220, "15M": 120, "1D": 100}


class MexcScanner:
    """Deterministic MEXC Futures scanner.

    The signal decision is driven by 4H regime + 1H structure/alignment +
    15M BOS/retest + structural risk/target geometry. 5M is not part of the
    decision path. Live order-book/deals endpoints are not polled per symbol;
    ticker data and closed-candle flow proxies are sufficient supporting data.
    """

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
        self._cache_limit = max(250, int(getattr(settings, "candle_limit", 250)))

    async def scan_once(self) -> dict[str, int]:
        symbols = await self.universe.refresh()
        if not symbols:
            return {"symbols": 0, "valid": 0, "sent": 0, "errors": 0}

        await self._refresh_btc_context()
        configured = int(getattr(self.settings, "scan_concurrency", 4))
        concurrency = max(1, min(4, configured))

        # Bounded workers avoid creating hundreds of suspended coroutine tasks.
        queue: asyncio.Queue[str] = asyncio.Queue()
        for symbol in symbols:
            queue.put_nowait(symbol)

        results: list[dict[str, Any] | Exception] = []
        completed = 0

        async def worker() -> None:
            nonlocal completed
            while True:
                try:
                    symbol = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    results.append(await self._scan_one(symbol))
                except Exception as exc:  # defensive; _scan_one also guards itself
                    results.append(exc)
                finally:
                    completed += 1
                    if completed == 1 or completed % 25 == 0 or completed == len(symbols):
                        LOGGER.info(
                            "MEXC scan progress: %d/%d symbols complete",
                            completed,
                            len(symbols),
                        )
                    queue.task_done()

        workers = [asyncio.create_task(worker(), name=f"mexc-scan-worker-{i}") for i in range(concurrency)]
        await queue.join()
        await asyncio.gather(*workers, return_exceptions=True)

        stats = {
            "symbols": len(symbols),
            "valid": 0,
            "sent": 0,
            "errors": 0,
            "technical_candidates": 0,
            "rejected_setup": 0,
            "rejected_futures": 0,
            "rejected_final": 0,
            "rejected_btc": 0,
            "rejected_quote": 0,
            "rejected_execution_quality": 0,
            "rejected_freshness": 0,
            "cache_hits": 0,
            "live_context_requests": 0,
        }
        for result in results:
            if isinstance(result, Exception):
                stats["errors"] += 1
                continue
            if result.get("valid"):
                stats["valid"] += 1
            if result.get("sent"):
                stats["sent"] += 1
            if result.get("error"):
                stats["errors"] += 1
            stats["cache_hits"] += int(result.get("candle_cache_hits", 0) or 0)
            stats["live_context_requests"] += int(result.get("live_context_requests", 0) or 0)
            stage = str(result.get("rejection_stage") or "")
            if stage == "TECHNICAL":
                stats["rejected_setup"] += 1
            elif stage == "FUTURES":
                stats["rejected_futures"] += 1
            elif stage == "BTC":
                stats["rejected_btc"] += 1
            elif stage == "QUOTE":
                stats["rejected_quote"] += 1
            elif stage == "FRESHNESS":
                stats["rejected_freshness"] += 1
            elif stage == "EXECUTION_QUALITY":
                stats["rejected_execution_quality"] += 1
            elif stage:
                stats["rejected_final"] += 1
            if result.get("technical_candidate"):
                stats["technical_candidates"] += 1

        LOGGER.info(
            "MEXC scan complete: symbols=%s valid=%s sent=%s errors=%s "
            "technical_candidates=%s cache_hits=%s live_context_requests=%s "
            "rejected_technical=%s rejected_btc=%s rejected_quote=%s "
            "rejected_freshness=%s rejected_execution=%s rejected_final=%s",
            stats["symbols"],
            stats["valid"],
            stats["sent"],
            stats["errors"],
            stats["technical_candidates"],
            stats["cache_hits"],
            stats["live_context_requests"],
            stats["rejected_setup"],
            stats["rejected_btc"],
            stats["rejected_quote"],
            stats["rejected_freshness"],
            stats["rejected_execution_quality"],
            stats["rejected_final"],
        )
        return stats

    async def _get_closed_candles(self, symbol: str, timeframe: str, limit: int) -> tuple[list[Any], bool]:
        """Return closed candles, refreshing only when a new closed bar is due."""
        tf = timeframe.upper()
        interval_ms = TIMEFRAME_MS[tf]
        key = (symbol.upper(), tf)
        now_ms = int(time.time() * 1000)
        expected_closed = (now_ms // interval_ms) * interval_ms - interval_ms
        cached = self._candle_cache.get(key)
        if cached and cached[0] >= expected_closed and len(cached[1]) >= min(limit, 80):
            return cached[1], True

        lock = self._candle_fetch_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._candle_cache.get(key)
            if cached and cached[0] >= expected_closed and len(cached[1]) >= min(limit, 80):
                return cached[1], True

            raw = await self.client.get_klines(symbol, MEXC_INTERVALS[tf], limit)
            rows = closed_candle_rows(raw, tf.lower())
            if rows:
                self._candle_cache[key] = (int(rows[-1]["time"]), rows)
            return rows, False

    async def _refresh_btc_context(self) -> None:
        try:
            c4, _ = await self._get_closed_candles("BTC_USDT", "4H", 250)
            c1, _ = await self._get_closed_candles("BTC_USDT", "1H", 250)
            c15, _ = await self._get_closed_candles("BTC_USDT", "15M", 250)
            if len(c4) < 205 or len(c1) < 205 or len(c15) < 80:
                self._btc_context = {"ok": False, "reason": "insufficient BTC history"}
                return
            self._btc_context = build_btc_context(c4, c1, c15)
        except Exception as exc:
            LOGGER.warning("BTC market context unavailable: %s", exc)
            self._btc_context = {"ok": False, "reason": str(exc)}

    async def _scan_one(self, symbol: str) -> dict[str, Any]:
        cache_hits = 0
        live_context_requests = 0
        try:
            # Stage 1: 15M structure is the cheapest and most selective setup
            # discovery step. Do not spend 4H/1H/1D requests on symbols with no
            # current BOS/retest opportunity.
            c15, hit15 = await self._get_closed_candles(
                symbol, "15M", min(self._cache_limit, CANDLE_LIMITS["15M"])
            )
            cache_hits += int(hit15)
            if len(c15) < 80:
                return self._reject(symbol, "Insufficient closed 15M candles", stage="DATA", analysis={})

            bos_events_long = _bos_events(c15, "LONG")
            bos_events_short = _bos_events(c15, "SHORT")
            bos_long, ret_long = _select_latest_bos_with_retest(c15, "LONG", bos_events_long)
            bos_short, ret_short = _select_latest_bos_with_retest(c15, "SHORT", bos_events_short)
            has_structure_setup = bool(
                (bos_long and ret_long.get("valid"))
                or (bos_short and ret_short.get("valid"))
            )
            if not has_structure_setup:
                return self._reject(
                    symbol,
                    "No current 15M BOS + retest structure",
                    stage="TECHNICAL",
                    analysis={
                        "long_bos_event_count": len(bos_events_long),
                        "short_bos_event_count": len(bos_events_short),
                    },
                )

            # Stage 2: only structurally active symbols pay for the HTF direction
            # check. This is the main API-load reduction.
            c4, hit4 = await self._get_closed_candles(
                symbol, "4H", min(self._cache_limit, CANDLE_LIMITS["4H"])
            )
            c1, hit1 = await self._get_closed_candles(
                symbol, "1H", min(self._cache_limit, CANDLE_LIMITS["1H"])
            )
            cache_hits += int(hit4) + int(hit1)
            for candles, minimum, label in ((c4, 205, "4H"), (c1, 205, "1H")):
                if len(candles) < minimum:
                    return self._reject(symbol, f"Insufficient closed {label} candles", stage="DATA", analysis={})

            regime = _four_hour_regime(c4)
            alignment = _one_hour_alignment(c1, regime)
            long_candidate = bool(alignment.get("long") and bos_long and ret_long.get("valid"))
            short_candidate = bool(alignment.get("short") and bos_short and ret_short.get("valid"))
            technical_direction_ok = (
                (long_candidate and _direction_aligned("LONG", regime, alignment))
                or (short_candidate and _direction_aligned("SHORT", regime, alignment))
            )
            if not technical_direction_ok:
                return self._reject(
                    symbol,
                    "4H/1H/15M structural direction not aligned",
                    stage="TECHNICAL",
                    analysis={
                        "trend_4h": regime.get("regime", "NO_TRADE"),
                        "one_hour_long_votes": alignment.get("long_votes", 0),
                        "one_hour_short_votes": alignment.get("short_votes", 0),
                        "long_bos_event_count": len(bos_events_long),
                        "short_bos_event_count": len(bos_events_short),
                        "long_retest": bool(ret_long.get("valid")),
                        "short_retest": bool(ret_short.get("valid")),
                    },
                )

            if long_candidate and not short_candidate:
                trigger_side = "LONG"
            elif short_candidate and not long_candidate:
                trigger_side = "SHORT"
            else:
                trigger_side = (
                    "LONG"
                    if float((bos_long or {}).get("strength") or 0.0)
                    >= float((bos_short or {}).get("strength") or 0.0)
                    else "SHORT"
                )

            # Stage 3: structural target path. 1D is used only after a direction
            # candidate exists because it is expensive relative to local analysis.
            c1d, hit1d = await self._get_closed_candles(
                symbol, "1D", min(self._cache_limit, CANDLE_LIMITS["1D"])
            )
            cache_hits += int(hit1d)
            analysis = analyze_candles(
                symbol,
                c4,
                c1,
                c15,
                candles_1d=c1d,
            )
            analysis.update(
                {
                    "mexc_4h_rows": c4,
                    "mexc_1h_rows": c1,
                    "mexc_15m_rows": c15,
                    "mexc_1d_rows": c1d,
                    "closed_4h_candles": len(c4),
                    "closed_1h_candles": len(c1),
                    "closed_15m_candles": len(c15),
                    "closed_1d_candles": len(c1d),
                    "signal_basis": "4H regime + 1H structure + 15M BOS/retest + ATR/structural geometry",
                }
            )

            setup = str(analysis.get("setup") or "NO TRADE").upper()
            LOGGER.debug(
                "MEXC ANALYSIS | %s | setup=%s score=%s rr=%s failures=%s",
                symbol,
                setup,
                analysis.get("score"),
                analysis.get("rr"),
                analysis.get("technical_gate_failures", []),
            )
            if setup not in {"LONG", "SHORT"}:
                return self._reject(symbol, "Analysis engine produced no LONG/SHORT setup", stage="TECHNICAL", analysis=analysis)

            btc_ok, btc_reason = btc_filter_ok(
                setup,
                self._btc_context,
                is_btc=(symbol.upper() == "BTC_USDT"),
            )
            analysis["btc_filter_ok"] = btc_ok
            analysis["btc_filter_reason"] = btc_reason
            analysis["btc_context"] = self._btc_context
            if not btc_ok:
                return self._reject(symbol, btc_reason, stage="BTC", analysis=analysis)

            ticker = await self.client.get_ticker(symbol)
            live_context_requests += 1
            if not ticker:
                return self._reject(symbol, "Missing MEXC ticker", stage="QUOTE", analysis=analysis)

            now_ms = int(time.time() * 1000)
            ts = self._safe_int(ticker.get("timestamp") or ticker.get("ts") or ticker.get("time"))
            if 0 < ts < 10**12:
                ts *= 1000
            max_age_ms = int(float(getattr(self.settings, "max_data_age_seconds", 5.0)) * 1000)
            data_fresh = bool(ts > 0 and abs(now_ms - ts) <= max_age_ms)
            analysis["ticker_timestamp"] = ts
            analysis["data_fresh"] = data_fresh
            if not data_fresh:
                return self._reject(symbol, "MEXC ticker is stale", stage="FRESHNESS", analysis=analysis)

            bid = self._safe_float(ticker.get("bid1") or ticker.get("bidPrice") or ticker.get("bid"))
            ask = self._safe_float(ticker.get("ask1") or ticker.get("askPrice") or ticker.get("ask"))
            last = self._safe_float(ticker.get("lastPrice") or ticker.get("last") or ticker.get("price"))
            if bid <= 0 or ask <= 0 or ask < bid:
                return self._reject(symbol, "Invalid MEXC bid/ask", stage="QUOTE", analysis=analysis)

            executable = ask if setup == "LONG" else bid
            mid = (bid + ask) / 2.0
            spread = abs(ask - bid) / mid if mid > 0 else float("inf")
            analysis.update(
                {
                    "mexc_bid": bid,
                    "mexc_ask": ask,
                    "mexc_last": last,
                    "mexc_spread_pct": spread,
                    "max_mexc_spread_pct": float(getattr(self.settings, "max_mexc_spread_pct", 0.001)),
                }
            )
            if spread > analysis["max_mexc_spread_pct"]:
                return self._reject(symbol, "MEXC spread too high", stage="EXECUTION_QUALITY", analysis=analysis)

            # MEXC's ticker already carries the index/fair/funding fields used as
            # context. Do not create fallback HTTP calls for every symbol.
            index_price = self._safe_float(ticker.get("indexPrice") or ticker.get("index"))
            fair_price = self._safe_float(ticker.get("fairPrice") or ticker.get("fair") or ticker.get("markPrice"))
            funding = self._safe_float_or_none(ticker.get("fundingRate"))
            reference = fair_price if fair_price > 0 else index_price if index_price > 0 else mid
            dislocation = abs(executable - reference) / reference if reference > 0 else 0.0
            analysis.update(
                {
                    "mexc_index_price": index_price,
                    "mexc_fair_price": fair_price,
                    "mexc_funding_rate": funding,
                    "mexc_reference_dislocation_pct": dislocation,
                    "funding_available": funding is not None,
                    "live_orderbook_polled": False,
                    "live_deals_polled": False,
                    "live_context_source": "ticker + closed-candle analysis",
                    "hold_vol": self._safe_float(ticker.get("holdVol") or ticker.get("holdVolume")),
                    "estimated_round_trip_cost_pct": float(getattr(self.settings, "estimated_round_trip_cost_pct", 0.0015)),
                }
            )

            self._update_confirmation_families(analysis)
            analysis["score"], analysis["score_groups"] = self._recalculate_score(analysis)
            analysis["technical_candidate"] = bool(
                setup in {"LONG", "SHORT"}
                and analysis.get("direction_ok")
                and analysis.get("structure_ok")
                and analysis.get("setup_ok")
                and analysis.get("location_ok")
                and analysis.get("risk_ok")
                and analysis.get("shock_veto_ok")
                and int(analysis.get("score", 0) or 0) >= int(getattr(self.settings, "min_confluence", 72))
            )

            planned_entry = self._safe_float(analysis.get("entry"))
            if planned_entry <= 0:
                return self._reject(symbol, "Invalid planned entry", stage="LEVELS", analysis=analysis)
            atr_value = self._safe_float(analysis.get("atr"))
            drift_atr = abs(executable - planned_entry) / atr_value if atr_value > 0 else float("inf")
            max_drift_atr = max(0.25, float(getattr(self.settings, "max_entry_drift_atr", 0.75)))
            analysis.update(
                {
                    "planned_entry": planned_entry,
                    "entry_drift_atr": drift_atr,
                    "max_entry_drift_atr": max_drift_atr,
                }
            )
            geometry_ok, geometry_reason = self._validate_live_geometry(
                analysis,
                executable,
                setup,
                max_drift_atr=max_drift_atr,
            )
            if not geometry_ok:
                return self._reject(symbol, geometry_reason, stage="EXECUTION_QUALITY", analysis=analysis)

            analysis["entry"] = float(executable)
            analysis["execution_entry"] = float(executable)
            analysis["executable_entry"] = float(executable)
            analysis["max_signal_age_seconds"] = float(getattr(self.settings, "max_signal_age_seconds", 1200.0))

            validated, reasons = validate_signal(
                analysis,
                min_confluence=int(getattr(self.settings, "min_confluence", 72)),
                min_rr=float(getattr(self.settings, "min_rr", 2.0)),
                require_increasing_volume=bool(getattr(self.settings, "require_increasing_volume", False)),
            )
            if validated is None:
                return self._reject(symbol, reasons, stage="FINAL_VALIDATOR", analysis=analysis)

            sent = False
            if bool(getattr(self.settings, "auto_signal_enabled", False)):
                sent = await self._publish_signal(validated)
            if (
                bool(getattr(self.settings, "auto_trade_enabled", False))
                and bool(getattr(self.settings, "allow_live_execution", False))
                and self.executor is not None
            ):
                await self._execute_signal(validated)
            return {
                "valid": True,
                "sent": sent,
                "error": False,
                "symbol": symbol,
                "analysis": analysis,
                "signal": validated,
                "technical_candidate": True,
                "candle_cache_hits": cache_hits,
                "live_context_requests": live_context_requests,
            }

        except Exception as exc:
            LOGGER.warning("MEXC scan failed for %s: %s", symbol, exc)
            return {
                "valid": False,
                "sent": False,
                "error": True,
                "symbol": symbol,
                "reason": str(exc),
                "rejection_stage": "ERROR",
                "candle_cache_hits": cache_hits,
                "live_context_requests": live_context_requests,
            }

    @staticmethod
    def _calculate_trade_flow(deals: list[dict[str, Any]]) -> dict[str, float]:
        """Pure compatibility helper; live scanner no longer polls recent deals."""
        buy_volume = 0.0
        sell_volume = 0.0
        for item in deals or []:
            try:
                volume = abs(float(item.get("v", item.get("vol", item.get("volume", 0))) or 0.0))
            except (TypeError, ValueError):
                volume = 0.0
            marker = str(item.get("T", item.get("side", ""))).upper()
            if marker in {"1", "BUY", "BID", "LONG"}:
                buy_volume += volume
            elif marker in {"2", "SELL", "ASK", "SHORT"}:
                sell_volume += volume
        return {"buy_volume": buy_volume, "sell_volume": sell_volume, "volume_delta": buy_volume - sell_volume}

    @staticmethod
    def _update_confirmation_families(analysis: dict[str, Any]) -> None:
        family_result = evaluate_confirmation_families(analysis)
        analysis["confirmation_families"] = family_result.get("families", {})
        analysis["confirmation_families_passed"] = int(family_result.get("passed", 0))
        analysis["confirmation_families_available"] = int(family_result.get("available", 0))
        analysis["confirmation_family_diversity_ok"] = bool(family_result.get("diversity_ok"))
        analysis["confirmation_families_passed_ok"] = True  # informational only; never a hard gate
        analysis["confirmation_family_count"] = int(family_result.get("passed", 0))
        analysis["supporting_family_count"] = int(family_result.get("passed", 0))
        analysis["supporting_quality_ok"] = bool(family_result.get("supporting_quality_ok", False))

    @staticmethod
    def _recalculate_score(analysis: dict[str, Any]) -> tuple[int, dict[str, int]]:
        """Score structure first, then supporting analysis evidence.

        No single optional live-context family can veto a structural setup.
        """
        setup_quality = max(
            0.0,
            min(
                1.0,
                0.55 * MexcScanner._safe_float(analysis.get("trigger_quality"))
                + 0.30 * MexcScanner._safe_float(analysis.get("bos_15m_strength"))
                + 0.15 * MexcScanner._safe_float((analysis.get("retest") or {}).get("quality")),
            ),
        )
        momentum_quality = min(1.0, MexcScanner._safe_float(analysis.get("momentum_ok")))
        volume_quality = min(1.0, MexcScanner._safe_float(analysis.get("volume_ok")))
        volatility_quality = min(1.0, MexcScanner._safe_float(analysis.get("volatility_ok")))
        trigger_quality = min(1.0, MexcScanner._safe_float(analysis.get("trigger_quality")))
        analysis_quality = (
            0.35 * momentum_quality
            + 0.25 * volume_quality
            + 0.20 * volatility_quality
            + 0.20 * trigger_quality
        )
        structure_points = 50 if all(
            bool(analysis.get(k))
            for k in ("direction_ok", "structure_ok", "setup_ok", "structure_quality_ok")
        ) else 0
        setup_points = int(round(15 * setup_quality)) if analysis.get("setup_ok") else 0
        analysis_points = int(round(20 * analysis_quality))
        target_points = 10 if analysis.get("location_ok") and analysis.get("target_path_structural") else 0
        risk_points = 5 if analysis.get("risk_ok") and analysis.get("direction_ok") else 0
        groups = {
            "structure_prerequisites": structure_points,
            "setup_quality": setup_points,
            "analysis_evidence": analysis_points,
            "structural_target": target_points,
            "risk_geometry": risk_points,
        }
        return max(0, min(100, sum(groups.values()))), groups

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_float_or_none(value: Any) -> float | None:
        try:
            value = float(value)
            return value if value == value else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(float(value)) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _futures_context_ok(analysis: dict[str, Any], side: str) -> bool:
        # Kept for compatibility with older callers. The current strategy does not
        # require microstructure direction as a signal gate.
        imbalance = float(analysis.get("orderbook_imbalance", 0.0) or 0.0)
        flow = float(analysis.get("volume_delta_ratio", 0.0) or 0.0)
        threshold = 0.05
        if side == "LONG":
            return imbalance >= threshold or flow >= threshold
        if side == "SHORT":
            return imbalance <= -threshold or flow <= -threshold
        return False

    @staticmethod
    def _validate_live_geometry(
        analysis: dict[str, Any],
        executable_price: float,
        side: str,
        *,
        max_drift_atr: float = 0.75,
    ) -> tuple[bool, str]:
        """Revalidate structural SL/TP at the executable quote using ATR geometry."""
        try:
            planned_entry = float(analysis["entry"])
            stop = float(analysis["stop_loss"])
            tp = float(analysis["tp"])
            atr_value = float(analysis.get("atr") or 0.0)
        except (KeyError, TypeError, ValueError):
            return False, "Missing or invalid structural SL/TP"

        if executable_price <= 0 or planned_entry <= 0 or atr_value <= 0:
            return False, "Invalid executable entry or ATR"
        if side == "LONG" and not (stop < planned_entry < tp):
            return False, "Invalid planned LONG geometry"
        if side == "SHORT" and not (tp < planned_entry < stop):
            return False, "Invalid planned SHORT geometry"

        drift_atr = abs(executable_price - planned_entry) / atr_value
        if drift_atr > max(0.0, float(max_drift_atr)):
            return False, f"Executable entry drift {drift_atr:.2f} ATR exceeds {max_drift_atr:.2f} ATR"
        if side == "LONG" and not (stop < executable_price < tp):
            return False, "Executable LONG geometry is no longer valid"
        if side == "SHORT" and not (tp < executable_price < stop):
            return False, "Executable SHORT geometry is no longer valid"

        risk = abs(executable_price - stop)
        reward = abs(tp - executable_price)
        if risk <= 0 or reward <= 0:
            return False, "Executable price invalidates structural geometry"

        stop_atr = risk / atr_value
        tp_atr = reward / atr_value
        if stop_atr < MIN_SL_ATR or stop_atr > MAX_SL_ATR:
            return False, f"Live SL {stop_atr:.2f} ATR outside {MIN_SL_ATR:.2f}-{MAX_SL_ATR:.2f}"
        if tp_atr < MIN_TP_ATR:
            return False, f"Live TP {tp_atr:.2f} ATR below minimum {MIN_TP_ATR:.2f}"

        cost_pct = float(analysis.get("estimated_round_trip_cost_pct", 0.0015) or 0.0015)
        funding = analysis.get("mexc_funding_rate")
        if funding is not None:
            cost_pct += min(0.0010, abs(float(funding)) * 2.0)
        try:
            net_rr = calculate_rr_after_costs(
                side=side,
                entry=executable_price,
                stop_loss=stop,
                target=tp,
                round_trip_cost_pct=cost_pct,
            )
        except (TypeError, ValueError):
            return False, "Unable to calculate live post-cost RR"
        if net_rr + 1e-12 < MIN_RR:
            return False, f"Live post-cost RR {net_rr:.2f} below minimum {MIN_RR:.2f}"

        analysis.update(
            {
                "entry_drift_atr": drift_atr,
                "trade_geometry_ok": True,
                "stop_distance_pct": risk / executable_price,
                "sl_atr": stop_atr,
                "tp_distance_atr": tp_atr,
                "tp_distance_pct": reward / executable_price,
                "rr_gross": reward / risk,
                "rr_net": net_rr,
                "rr": net_rr,
                "live_geometry_reason": "OK",
            }
        )
        return True, "OK"

    @staticmethod
    def _reject(
        symbol: str,
        reason: Any,
        *,
        stage: str,
        analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = analysis if analysis is not None else {}
        payload["rejection_stage"] = stage
        payload["rejection_reasons"] = reason if isinstance(reason, list) else [str(reason)]
        # Per-symbol rejection logging is DEBUG: INFO only reports cycle-level status.
        LOGGER.debug(
            "MEXC REJECT | %s | stage=%s | reason=%s | setup=%s | score=%s | rr=%s",
            symbol,
            stage,
            payload["rejection_reasons"],
            payload.get("setup", "NO TRADE"),
            payload.get("score"),
            payload.get("rr"),
        )
        return {
            "valid": False,
            "sent": False,
            "error": False,
            "symbol": symbol,
            "reason": reason,
            "analysis": payload,
            "rejection_stage": stage,
            "technical_candidate": bool(payload.get("technical_candidate", False)),
            "candle_cache_hits": 0,
            "live_context_requests": 0,
        }

    async def _publish_signal(self, signal):
        try:
            return bool(await self.signal_manager.publish(signal))
        except Exception:
            LOGGER.exception("Signal publication failed for %s", signal.symbol)
            return False

    async def _execute_signal(self, signal):
        if self.executor is None:
            return None
        meta = self.universe.get(signal.symbol) if hasattr(self.universe, "get") else None
        if meta is None:
            return None
        return await self.executor.execute(signal, meta)
