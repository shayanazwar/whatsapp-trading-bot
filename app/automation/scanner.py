from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..analysis.engine import (
    analyze_candles,
    btc_filter_ok,
    build_btc_context,
    closed_candle_rows,
)
from ..config import Settings
from .executor import MexcExecutor
from .mexc_client import MexcClient
from .signal_manager import SignalManager
from .signal_validator import validate_signal
from .universe import MexcUniverse

LOGGER = logging.getLogger(__name__)

MEXC_INTERVALS = {"4H": "Hour4", "1H": "Min60", "15M": "Min15", "5M": "Min5", "1D": "Day1"}


class MexcScanner:
    """Deterministic MEXC Futures scanner with technical + live-context gates."""

    def __init__(self, *, settings: Settings, client: MexcClient, universe: MexcUniverse, signal_manager: SignalManager, executor: MexcExecutor | None = None) -> None:
        self.settings = settings
        self.client = client
        self.universe = universe
        self.signal_manager = signal_manager
        self.executor = executor
        self._btc_context: dict[str, Any] = {"ok": False, "reason": "not loaded"}

    async def scan_once(self) -> dict[str, int]:
        symbols = await self.universe.refresh()
        if not symbols:
            return {"symbols": 0, "valid": 0, "sent": 0, "errors": 0}

        await self._refresh_btc_context()
        concurrency = max(1, int(getattr(self.settings, "scan_concurrency", 8)))
        semaphore = asyncio.Semaphore(concurrency)
        results = await asyncio.gather(*(self._scan_one(symbol, semaphore) for symbol in symbols), return_exceptions=True)

        stats: dict[str, int] = {"symbols": len(symbols), "valid": 0, "sent": 0, "errors": 0, "technical_candidates": 0, "rejected_setup": 0, "rejected_futures": 0, "rejected_final": 0}
        for result in results:
            if isinstance(result, Exception):
                stats["errors"] += 1
                continue
            if not isinstance(result, dict):
                continue
            if result.get("valid"): stats["valid"] += 1
            if result.get("sent"): stats["sent"] += 1
            if result.get("error"): stats["errors"] += 1
            stage = str(result.get("rejection_stage") or "")
            if stage == "TECHNICAL": stats["rejected_setup"] += 1
            elif stage == "FUTURES": stats["rejected_futures"] += 1
            elif stage: stats["rejected_final"] += 1
            if result.get("technical_candidate"): stats["technical_candidates"] += 1

        LOGGER.info("MEXC scan complete: symbols=%s valid=%s sent=%s errors=%s technical_candidates=%s rejected_setup=%s rejected_futures=%s rejected_final=%s", stats["symbols"], stats["valid"], stats["sent"], stats["errors"], stats["technical_candidates"], stats["rejected_setup"], stats["rejected_futures"], stats["rejected_final"])
        return stats

    async def _refresh_btc_context(self) -> None:
        try:
            raw4, raw1, raw15 = await asyncio.gather(
                self.client.get_klines("BTC_USDT", MEXC_INTERVALS["4H"], 250),
                self.client.get_klines("BTC_USDT", MEXC_INTERVALS["1H"], 250),
                self.client.get_klines("BTC_USDT", MEXC_INTERVALS["15M"], 250),
            )
            c4 = closed_candle_rows(raw4, "4h")
            c1 = closed_candle_rows(raw1, "1h")
            c15 = closed_candle_rows(raw15, "15m")
            if len(c4) < 205 or len(c1) < 205 or len(c15) < 80:
                self._btc_context = {"ok": False, "reason": "insufficient BTC history"}
                return
            self._btc_context = build_btc_context(c4, c1, c15)
        except Exception as exc:
            LOGGER.warning("BTC market context unavailable: %s", exc)
            self._btc_context = {"ok": False, "reason": str(exc)}

    async def _scan_one(self, symbol: str, semaphore: asyncio.Semaphore) -> dict[str, Any]:
        async with semaphore:
            try:
                limit = max(250, int(getattr(self.settings, "candle_limit", 250)))
                raw4, raw1, raw15, raw5 = await asyncio.gather(
                    self.client.get_klines(symbol, MEXC_INTERVALS["4H"], limit),
                    self.client.get_klines(symbol, MEXC_INTERVALS["1H"], limit),
                    self.client.get_klines(symbol, MEXC_INTERVALS["15M"], limit),
                    self.client.get_klines(symbol, MEXC_INTERVALS["5M"], limit),
                )
                c4 = closed_candle_rows(raw4, "4h"); c1 = closed_candle_rows(raw1, "1h"); c15 = closed_candle_rows(raw15, "15m"); c5 = closed_candle_rows(raw5, "5m")
                for candles, minimum, label in ((c4, 205, "4H"), (c1, 205, "1H"), (c15, 80, "15M"), (c5, 30, "5M")):
                    if len(candles) < minimum:
                        return self._reject(symbol, f"Insufficient closed {label} candles", stage="DATA", analysis={})

                # 1D is fetched only after the core 4H/1H/15M/5M data are usable.
                raw1d = await self.client.get_klines(symbol, MEXC_INTERVALS["1D"], 60)
                c1d = closed_candle_rows(raw1d, "1d")
                analysis = analyze_candles(symbol, c4, c1, c15, c5, c1d)
                analysis.update({"mexc_4h_rows": c4, "mexc_1h_rows": c1, "mexc_15m_rows": c15, "mexc_5m_rows": c5, "mexc_1d_rows": c1d, "closed_4h_candles": len(c4), "closed_1h_candles": len(c1), "closed_15m_candles": len(c15), "closed_5m_candles": len(c5), "closed_5m_candle_time": int(c5[-1]["time"])})

                setup = str(analysis.get("setup") or "NO TRADE").upper()
                LOGGER.info("MEXC ANALYSIS | %s | setup=%s | score=%s | rr=%s | stage=%s | failures=%s", symbol, setup, analysis.get("score"), analysis.get("rr"), analysis.get("rejection_stage") or "CANDIDATE", analysis.get("technical_gate_failures", []))
                if setup not in {"LONG", "SHORT"}:
                    return self._reject(symbol, "Analysis engine produced no valid LONG/SHORT setup", stage="TECHNICAL", analysis=analysis)

                # BTC/global filter is evaluated before spending extra live-context calls.
                btc_ok, btc_reason = btc_filter_ok(setup, self._btc_context, is_btc=(symbol.upper() == "BTC_USDT"))
                analysis["btc_filter_ok"] = btc_ok; analysis["btc_filter_reason"] = btc_reason; analysis["btc_context"] = self._btc_context
                if not btc_ok:
                    return self._reject(symbol, btc_reason, stage="BTC", analysis=analysis)

                ticker = await self.client.get_ticker(symbol)
                if not ticker:
                    return self._reject(symbol, "Missing MEXC ticker", stage="QUOTE", analysis=analysis)
                now_ms = int(time.time() * 1000)
                ts = self._safe_int(ticker.get("timestamp") or ticker.get("ts") or ticker.get("time"))
                max_age_ms = int(float(getattr(self.settings, "max_data_age_seconds", 5.0)) * 1000)
                data_fresh = bool(ts > 0 and abs(now_ms - ts) <= max_age_ms)
                analysis["ticker_timestamp"] = ts; analysis["data_fresh"] = data_fresh
                if not data_fresh:
                    return self._reject(symbol, "MEXC ticker is stale", stage="FRESHNESS", analysis=analysis)

                bid = self._safe_float(ticker.get("bid1") or ticker.get("bidPrice") or ticker.get("bid"))
                ask = self._safe_float(ticker.get("ask1") or ticker.get("askPrice") or ticker.get("ask"))
                last = self._safe_float(ticker.get("lastPrice") or ticker.get("last") or ticker.get("price"))
                if bid <= 0 or ask <= 0 or ask < bid:
                    return self._reject(symbol, "Invalid MEXC bid/ask", stage="QUOTE", analysis=analysis)
                executable = ask if setup == "LONG" else bid
                mid = (bid + ask) / 2.0
                spread = abs(ask - bid) / mid if mid > 0 else 999.0
                analysis.update({"mexc_bid": bid, "mexc_ask": ask, "mexc_last": last, "mexc_spread_pct": spread})
                if spread > float(getattr(self.settings, "max_mexc_spread_pct", 0.001)):
                    return self._reject(symbol, "MEXC spread too high", stage="EXECUTION_QUALITY", analysis=analysis)

                index_price = self._safe_float(ticker.get("indexPrice") or ticker.get("index"))
                fair_price = self._safe_float(ticker.get("fairPrice") or ticker.get("fair") or ticker.get("markPrice"))
                funding = self._safe_float_or_none(ticker.get("fundingRate"))
                if index_price <= 0:
                    index_data = await self._safe_call(self.client.get_index_price, symbol)
                    index_price = self._safe_float(index_data.get("indexPrice") or index_data.get("index")) if index_data else 0.0
                if fair_price <= 0:
                    fair_data = await self._safe_call(self.client.get_fair_price, symbol)
                    fair_price = self._safe_float(fair_data.get("fairPrice") or fair_data.get("fair")) if fair_data else 0.0
                if funding is None:
                    funding_data = await self._safe_call(self.client.get_funding_rate, symbol)
                    funding = self._safe_float_or_none((funding_data or {}).get("fundingRate") or (funding_data or {}).get("rate"))
                reference = fair_price if fair_price > 0 else index_price
                if reference <= 0:
                    return self._reject(symbol, "Missing MEXC index/fair reference", stage="EXECUTION_QUALITY", analysis=analysis)
                dislocation = abs(executable - reference) / reference
                analysis.update({"mexc_index_price": index_price, "mexc_fair_price": fair_price, "mexc_funding_rate": funding, "mexc_reference_dislocation_pct": dislocation})
                if dislocation > float(getattr(self.settings, "max_index_dislocation_pct", 0.002)):
                    return self._reject(symbol, "MEXC executable price is too far from index/fair", stage="EXECUTION_QUALITY", analysis=analysis)

                depth, deals = await asyncio.gather(self._safe_call(self.client.get_depth, symbol, int(getattr(self.settings, "orderbook_levels", 10))), self._safe_call(self.client.get_deals, symbol, int(getattr(self.settings, "trade_flow_limit", 100))))
                if depth:
                    analysis.update(self._calculate_depth(depth))
                if deals:
                    analysis.update(self._calculate_trade_flow(deals))
                analysis["hold_vol"] = self._safe_float(ticker.get("holdVol") or ticker.get("holdVolume"))
                analysis["funding_available"] = funding is not None

                futures_ok = self._futures_context_ok(analysis, setup)
                analysis["futures_ok"] = futures_ok
                analysis["futures_context"] = "AVAILABLE" if futures_ok else "FAILED"
                if not futures_ok:
                    return self._reject(symbol, "MEXC futures context failed", stage="FUTURES", analysis=analysis)

                self._update_confirmation_families(analysis)
                analysis["score"], analysis["score_groups"] = self._recalculate_score(analysis)

                planned_entry = self._safe_float(analysis.get("entry"))
                if planned_entry <= 0:
                    return self._reject(symbol, "Invalid planned entry", stage="LEVELS", analysis=analysis)
                drift = abs(executable - planned_entry) / planned_entry
                analysis["entry_drift_pct"] = drift
                if drift > float(getattr(self.settings, "max_entry_drift_pct", 0.002)):
                    return self._reject(symbol, "Executable entry drift exceeds limit", stage="EXECUTION_QUALITY", analysis=analysis)
                self._reprice_levels(analysis, executable)
                self._update_confirmation_families(analysis)
                analysis["score"], analysis["score_groups"] = self._recalculate_score(analysis)
                analysis["max_entry_drift_pct"] = float(getattr(self.settings, "max_entry_drift_pct", 0.002))
                analysis["max_signal_age_seconds"] = float(getattr(self.settings, "max_signal_age_seconds", 330.0))

                validated, reasons = validate_signal(analysis, min_confluence=int(getattr(self.settings, "min_confluence", 82)), min_rr=float(getattr(self.settings, "min_rr", 2.0)), require_increasing_volume=bool(getattr(self.settings, "require_increasing_volume", False)))
                if validated is None:
                    return self._reject(symbol, reasons, stage="FINAL_VALIDATOR", analysis=analysis)

                sent = False
                if bool(getattr(self.settings, "auto_signal_enabled", False)):
                    sent = await self._publish_signal(validated)
                # Executor remains hard-disabled by its own class gate.
                if bool(getattr(self.settings, "auto_trade_enabled", False)) and bool(getattr(self.settings, "allow_live_execution", False)) and self.executor is not None:
                    await self._execute_signal(validated)
                return {"valid": True, "sent": sent, "error": False, "symbol": symbol, "analysis": analysis, "signal": validated, "technical_candidate": True}

            except Exception as exc:
                LOGGER.exception("MEXC scan failed for %s", symbol)
                return {"valid": False, "sent": False, "error": True, "symbol": symbol, "reason": str(exc), "rejection_stage": "ERROR"}

    @staticmethod
    def _futures_context_ok(analysis: dict[str, Any], side: str) -> bool:
        if not bool(analysis.get("funding_available")):
            return False
        imbalance = float(analysis.get("orderbook_imbalance", 0.0) or 0.0)
        flow = float(analysis.get("volume_delta_ratio", 0.0) or 0.0)
        if side == "LONG":
            return imbalance >= 0.05 or flow >= 0.05
        return imbalance <= -0.05 or flow <= -0.05

    @staticmethod
    def _update_confirmation_families(analysis: dict[str, Any]) -> None:
        families = ("direction_ok", "structure_ok", "setup_ok", "momentum_ok", "volume_ok", "location_ok")
        analysis["confirmation_family_count"] = sum(bool(analysis.get(name, False)) for name in families)

    @staticmethod
    def _recalculate_score(analysis: dict[str, Any]) -> tuple[int, dict[str, int]]:
        groups = {
            "direction_regime": 20 if analysis.get("direction_ok") else 0,
            "market_structure": 20 if analysis.get("structure_ok") else 0,
            "setup_entry_trigger": 20 if analysis.get("setup_ok") else 0,
            "momentum": 10 if analysis.get("momentum_ok") else 0,
            "volume_participation": 10 if analysis.get("volume_ok") else 0,
            "location_target_path": 10 if analysis.get("location_ok") else 0,
            "futures_market_context": 5 if analysis.get("futures_ok") else 0,
            "volatility_execution": 5 if analysis.get("volatility_ok") else 0,
        }
        setup_q = float(analysis.get("trigger_quality_5m", 0.0) or 0.0)
        bos_q = float(analysis.get("bos_15m_strength", 0.0) or 0.0)
        ret_q = float((analysis.get("retest") or {}).get("quality", 0.0) or 0.0)
        quality = 0.50 * setup_q + 0.25 * bos_q + 0.25 * ret_q
        if groups["setup_entry_trigger"] and quality < 0.60:
            groups["setup_entry_trigger"] -= 5
        if groups["volume_participation"] and float(analysis.get("rvol_15m", 0.0) or 0.0) < 1.25:
            groups["volume_participation"] -= 2
        return max(0, min(100, sum(groups.values()))), groups

    @staticmethod
    def _safe_float(value: Any) -> float:
        try: return float(value) if value is not None else 0.0
        except (TypeError, ValueError): return 0.0

    @staticmethod
    def _safe_float_or_none(value: Any) -> float | None:
        try:
            x = float(value)
            return x if x == x else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value: Any) -> int:
        try: return int(float(value)) if value is not None else 0
        except (TypeError, ValueError): return 0

    @staticmethod
    async def _safe_call(fn, *args):
        try:
            result = await fn(*args)
            return result
        except Exception as exc:
            LOGGER.debug("Optional MEXC context call failed: %s", exc)
            return None

    @staticmethod
    def _calculate_depth(orderbook: dict[str, Any]) -> dict[str, float]:
        bids = orderbook.get("bids") or []; asks = orderbook.get("asks") or []
        def total(levels: list[Any]) -> float:
            value = 0.0
            for level in levels:
                try:
                    if isinstance(level, dict): qty = level.get("quantity") or level.get("qty") or level.get("volume") or level.get("v") or 0
                    else: qty = level[1] if len(level) > 1 else 0
                    value += float(qty)
                except Exception: continue
            return value
        bid_depth = total(bids); ask_depth = total(asks); total_depth = bid_depth + ask_depth
        return {"bid_depth": bid_depth, "ask_depth": ask_depth, "orderbook_imbalance": (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0}

    @staticmethod
    def _calculate_trade_flow(deals: list[Any]) -> dict[str, float]:
        buy = 0.0; sell = 0.0
        for deal in deals:
            if not isinstance(deal, dict): continue
            try:
                qty = float(deal.get("v") or deal.get("volume") or deal.get("vol") or deal.get("quantity") or 0)
                side = deal.get("T", deal.get("side", deal.get("type", "")))
                if str(side).lower() in {"1", "buy", "purchase", "bid"}: buy += qty
                elif str(side).lower() in {"2", "sell", "ask"}: sell += qty
            except Exception:
                continue
        total = buy + sell; delta = buy - sell
        return {"buy_volume": buy, "sell_volume": sell, "volume_delta": delta, "volume_delta_ratio": delta / total if total > 0 else 0.0}

    @staticmethod
    def _reprice_levels(analysis: dict[str, Any], executable_price: float) -> None:
        old_entry = float(analysis["entry"]); delta = executable_price - old_entry
        analysis["entry"] = executable_price
        analysis["stop_loss"] = float(analysis["stop_loss"]) + delta
        analysis["tp1"] = float(analysis["tp1"]) + delta
        analysis["tp2"] = float(analysis["tp2"]) + delta
        risk = abs(executable_price - float(analysis["stop_loss"]))
        reward = abs(float(analysis["tp2"]) - executable_price)
        analysis["rr"] = reward / risk if risk > 0 else 0.0

    @staticmethod
    def _reject(symbol: str, reason: Any, *, stage: str, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = analysis if analysis is not None else {}
        payload["rejection_stage"] = stage
        if isinstance(reason, list): payload["rejection_reasons"] = reason
        else: payload["rejection_reasons"] = [str(reason)]
        LOGGER.info("MEXC REJECT | %s | stage=%s | reason=%s | setup=%s | score=%s | rr=%s", symbol, stage, payload["rejection_reasons"], payload.get("setup", "NO TRADE"), payload.get("score"), payload.get("rr"))
        return {"valid": False, "sent": False, "error": False, "symbol": symbol, "reason": reason, "analysis": payload, "rejection_stage": stage, "technical_candidate": bool(payload.get("technical_candidate", False))}

    async def _publish_signal(self, signal):
        try: return bool(await self.signal_manager.publish(signal))
        except Exception: LOGGER.exception("Signal publication failed for %s", signal.symbol); return False

    async def _execute_signal(self, signal):
        if self.executor is None: return None
        meta = self.universe.get(signal.symbol) if hasattr(self.universe, "get") else None
        if meta is None: return None
        return await self.executor.execute(signal, meta)
