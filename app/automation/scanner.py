from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..analysis.engine import (
    _bos_events,
    _direction_aligned,
    _fifteen_minute_entry_confirmation,
    _four_hour_regime,
    _one_hour_alignment,
    _select_latest_bos_with_retest,
    analyze_candles,
    btc_filter_ok,
    build_btc_context,
    closed_candle_rows,
    MIN_RR,
    MIN_SL_ATR,
    MAX_SL_ATR,
    MIN_TP_ATR,
    MIN_CONFIRMATION_FAMILIES,
    MIN_AVAILABLE_CONFIRMATION_FAMILIES,
    evaluate_confirmation_families,
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
        configured_concurrency = int(getattr(self.settings, "scan_concurrency", 4))
        concurrency = max(1, min(4, configured_concurrency))
        semaphore = asyncio.Semaphore(concurrency)
        results = await asyncio.gather(*(self._scan_one(symbol, semaphore) for symbol in symbols), return_exceptions=True)

        stats: dict[str, int] = {
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
        }
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
            if result.get("technical_candidate"): stats["technical_candidates"] += 1

        LOGGER.info(
            "MEXC scan complete: symbols=%s valid=%s sent=%s errors=%s "
            "technical_candidates=%s rejected_setup=%s rejected_btc=%s "
            "rejected_futures=%s rejected_quote=%s rejected_freshness=%s "
            "rejected_execution_quality=%s rejected_final=%s",
            stats["symbols"], stats["valid"], stats["sent"], stats["errors"],
            stats["technical_candidates"], stats["rejected_setup"],
            stats["rejected_btc"], stats["rejected_futures"],
            stats["rejected_quote"], stats["rejected_freshness"],
            stats["rejected_execution_quality"], stats["rejected_final"],
        )
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

                # Stage 1: fetch the 4H/1H/15M setup context. 5M/1D is fetched only
                # after a causal 15M BOS/retest setup is found.
                raw4, raw1, raw15 = await asyncio.gather(
                    self.client.get_klines(symbol, MEXC_INTERVALS["4H"], limit),
                    self.client.get_klines(symbol, MEXC_INTERVALS["1H"], limit),
                    self.client.get_klines(symbol, MEXC_INTERVALS["15M"], limit),
                )
                c4 = closed_candle_rows(raw4, "4h")
                c1 = closed_candle_rows(raw1, "1h")
                c15 = closed_candle_rows(raw15, "15m")
                for candles, minimum, label in ((c4, 205, "4H"), (c1, 205, "1H"), (c15, 80, "15M")):
                    if len(candles) < minimum:
                        return self._reject(symbol, f"Insufficient closed {label} candles", stage="DATA", analysis={})

                # Semantics-preserving technical prefilter. These are the same
                # mandatory gates used by analyze_candles(); the full engine
                # remains authoritative after this prefilter.
                regime = _four_hour_regime(c4)
                alignment = _one_hour_alignment(c1, regime)
                bos_events_long = _bos_events(c15, "LONG")
                bos_events_short = _bos_events(c15, "SHORT")
                bos_long, ret_long = _select_latest_bos_with_retest(c15, "LONG", bos_events_long)
                bos_short, ret_short = _select_latest_bos_with_retest(c15, "SHORT", bos_events_short)
                long_candidate = bool(alignment.get("long") and bos_long and ret_long.get("valid"))
                short_candidate = bool(alignment.get("short") and bos_short and ret_short.get("valid"))
                technical_direction_ok = (
                    (long_candidate and _direction_aligned("LONG", regime, alignment))
                    or (short_candidate and _direction_aligned("SHORT", regime, alignment))
                )

                if not technical_direction_ok:
                    return self._reject(
                        symbol,
                        "Mandatory 4H/1H/15M technical gates not satisfied",
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
                        if float((bos_long or {}).get("strength") or 0.0) >= float((bos_short or {}).get("strength") or 0.0)
                        else "SHORT"
                    )
                active_bos = bos_long if trigger_side == "LONG" else bos_short
                active_retest = ret_long if trigger_side == "LONG" else ret_short
                trigger_level = float(active_bos["level"])
                retest_time = int(active_retest["time"])
                # 15M BOS/retest defines the setup. Quality is evaluated once by
                # the authoritative engine; the scanner must not duplicate it with
                # a stricter high-precision prefilter.

                # Stage 2: qualifying structural candidates need the 1D target
                # context. 5M remains optional refinement and cannot reject setup.
                raw5, raw1d = await asyncio.gather(
                    self.client.get_klines(symbol, MEXC_INTERVALS["5M"], limit),
                    self.client.get_klines(symbol, MEXC_INTERVALS["1D"], 60),
                )
                c5 = closed_candle_rows(raw5, "5m")
                c1d = closed_candle_rows(raw1d, "1d")
                # 5M is optional refinement. Missing/insufficient 5M data must not
                # reject a valid 15M intraday setup.
                analysis = analyze_candles(symbol, c4, c1, c15, c5 if len(c5) >= 30 else [], c1d)
                analysis.update({
                    "mexc_4h_rows": c4,
                    "mexc_1h_rows": c1,
                    "mexc_15m_rows": c15,
                    "mexc_5m_rows": c5,
                    "mexc_1d_rows": c1d,
                    "closed_4h_candles": len(c4),
                    "closed_1h_candles": len(c1),
                    "closed_15m_candles": len(c15),
                    "closed_5m_candles": len(c5),
                    "closed_5m_candle_time": int(c5[-1]["time"]) if c5 else 0,
                })

                setup = str(analysis.get("setup") or "NO TRADE").upper()
                LOGGER.info(
                    "MEXC ANALYSIS | %s | setup=%s | score=%s | rr=%s | "
                    "stage=%s | failures=%s | bos(L/S)=%s/%s | retest(L/S)=%s/%s | "
                    "trigger=%s",
                    symbol, setup, analysis.get("score"), analysis.get("rr"),
                    analysis.get("rejection_stage") or "CANDIDATE",
                    analysis.get("technical_gate_failures", []),
                    analysis.get("long_bos_event_count", 0),
                    analysis.get("short_bos_event_count", 0),
                    analysis.get("long_retest", False),
                    analysis.get("short_retest", False),
                    analysis.get("trigger_5m", "NONE"),
                )
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
                if 0 < ts < 10**12:
                    ts *= 1000
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
                analysis["futures_context"] = "AVAILABLE" if futures_ok else "INSUFFICIENT_DIRECTIONAL_CONFIRMATION"
                analysis["max_mexc_spread_pct"] = float(getattr(self.settings, "max_mexc_spread_pct", 0.001))
                analysis["estimated_round_trip_cost_pct"] = float(
                    getattr(self.settings, "estimated_round_trip_cost_pct", 0.0015)
                )

                # Re-evaluate the eight supporting families with real MEXC
                # execution/flow data. Missing optional families abstain.
                self._update_confirmation_families(analysis)
                analysis["score"], analysis["score_groups"] = self._recalculate_score(analysis)
                analysis["technical_candidate"] = bool(
                    setup in {"LONG", "SHORT"}
                    and analysis.get("direction_ok")
                    and analysis.get("structure_ok")
                    and analysis.get("setup_ok")
                    and analysis.get("location_ok")
                    and analysis.get("risk_ok")
                    and int(analysis.get("score", 0) or 0) >= int(getattr(self.settings, "min_confluence", 78))
                    and analysis.get("confirmation_families_passed_ok")
                    and int(analysis.get("confirmation_families_passed", 0)) >= MIN_CONFIRMATION_FAMILIES
                    and int(analysis.get("confirmation_families_available", 0)) >= MIN_AVAILABLE_CONFIRMATION_FAMILIES
                )

                planned_entry = self._safe_float(analysis.get("entry"))
                if planned_entry <= 0:
                    return self._reject(symbol, "Invalid planned entry", stage="LEVELS", analysis=analysis)
                max_entry_drift = float(getattr(self.settings, "max_entry_drift_pct", 0.002))
                drift = abs(executable - planned_entry) / planned_entry
                analysis["planned_entry"] = planned_entry
                analysis["entry_drift_pct"] = drift
                analysis["max_entry_drift_pct"] = max_entry_drift

                # No blind level translation. SL/TP remain attached to the
                # structural invalidation/target levels generated by the engine.
                geometry_ok, geometry_reason = self._validate_live_geometry(
                    analysis,
                    executable,
                    setup,
                    max_drift_pct=max_entry_drift,
                )
                if not geometry_ok:
                    return self._reject(symbol, geometry_reason, stage="EXECUTION_QUALITY", analysis=analysis)

                # The published Entry is the executable quote, while SL/TP are
                # unchanged structural levels. RR is recomputed, never faked.
                analysis["entry"] = float(executable)
                analysis["execution_entry"] = float(executable)
                analysis["executable_entry"] = float(executable)
                risk = abs(float(executable) - float(analysis["stop_loss"]))
                reward = abs(float(analysis["tp"]) - float(executable))
                analysis["rr"] = reward / risk if risk > 0 else 0.0
                analysis["max_signal_age_seconds"] = float(getattr(self.settings, "max_signal_age_seconds", 330.0))

                validated, reasons = validate_signal(analysis, min_confluence=int(getattr(self.settings, "min_confluence", 75)), min_rr=float(getattr(self.settings, "min_rr", 2.0)), require_increasing_volume=bool(getattr(self.settings, "require_increasing_volume", False)))
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
        """Return True only when live futures flow has directional agreement.

        Funding availability is contextual data, not a directional vote.
        """
        imbalance = float(analysis.get("orderbook_imbalance", 0.0) or 0.0)
        flow = float(analysis.get("volume_delta_ratio", 0.0) or 0.0)
        threshold = 0.05
        if side == "LONG":
            return imbalance >= threshold or flow >= threshold
        if side == "SHORT":
            return imbalance <= -threshold or flow <= -threshold
        return False

    @staticmethod
    def _update_confirmation_families(analysis: dict[str, Any]) -> None:
        family_result = evaluate_confirmation_families(analysis)
        analysis["confirmation_families"] = family_result.get("families", {})
        analysis["confirmation_families_passed"] = int(family_result.get("passed", 0))
        analysis["confirmation_families_available"] = int(family_result.get("available", 0))
        analysis["confirmation_family_diversity_ok"] = bool(family_result.get("diversity_ok"))
        analysis["confirmation_families_passed_ok"] = bool(family_result.get("passed_ok"))
        analysis["confirmation_family_count"] = int(family_result.get("passed", 0))
        analysis["supporting_family_count"] = int(family_result.get("passed", 0))
        analysis["min_confirmation_families"] = MIN_CONFIRMATION_FAMILIES
        analysis["min_available_confirmation_families"] = MIN_AVAILABLE_CONFIRMATION_FAMILIES

    @staticmethod
    def _recalculate_score(analysis: dict[str, Any]) -> tuple[int, dict[str, int]]:
        """Score uses structural prerequisites plus the eight-family evidence."""
        family_data = analysis.get("confirmation_families") or {}
        family_weights = {
            "momentum": 5,
            "relative_volume": 4,
            "volatility_regime": 4,
            "liquidity_quality": 4,
            "funding_crowding": 4,
            "flow_pressure": 5,
            "htf_target_path": 5,
            "vwap_location": 4,
        }
        family_points = sum(
            family_weights[name]
            for name, weight in family_weights.items()
            if isinstance(family_data.get(name), dict)
            and family_data[name].get("status") == "PASS"
        )
        trigger_q = max(0.0, min(1.0, float(analysis.get("trigger_quality") or 0.0)))
        bos_q = max(0.0, min(1.0, float(analysis.get("bos_15m_strength") or 0.0)))
        retest = analysis.get("retest") or {}
        retest_q = max(0.0, min(1.0, float(retest.get("quality") or 0.0)))
        setup_quality = 0.50 * trigger_q + 0.25 * bos_q + 0.25 * retest_q
        groups = {
            "structure_prerequisites": 35 if all(bool(analysis.get(k)) for k in ("direction_ok", "structure_ok", "setup_ok")) else 0,
            "setup_quality": int(round(10 * setup_quality)) if analysis.get("setup_ok") else 0,
            "family_evidence": family_points,
            "risk_geometry": 20 if analysis.get("trade_geometry_ok") else 0,
        }
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
    def _validate_live_geometry(
        analysis: dict[str, Any],
        executable_price: float,
        side: str,
        *,
        max_drift_pct: float = 0.002,
    ) -> tuple[bool, str]:
        """Revalidate one structural SL/TP at the executable quote."""
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

        drift = abs(executable_price - planned_entry) / planned_entry
        if drift > max(0.0, float(max_drift_pct)):
            return False, "Executable entry drift exceeds limit"

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
        rr = reward / risk
        if stop_atr < MIN_SL_ATR or stop_atr > MAX_SL_ATR:
            return False, f"Live SL {stop_atr:.2f} ATR outside {MIN_SL_ATR:.2f}-{MAX_SL_ATR:.2f}"
        if tp_atr < MIN_TP_ATR:
            return False, f"Live TP {tp_atr:.2f} ATR below minimum {MIN_TP_ATR:.2f}"

        # Reuse the same conservative cost allowance as the final validator.
        try:
            from .signal_validator import MIN_RR as VALIDATOR_MIN_RR
            from ..automation.risk_manager import calculate_rr_after_costs
            cost_pct = float(analysis.get("estimated_round_trip_cost_pct", 0.0015) or 0.0015)
            funding = analysis.get("mexc_funding_rate")
            if funding is not None:
                cost_pct += min(0.0010, abs(float(funding)) * 2.0)
            net_rr = calculate_rr_after_costs(
                side=side,
                entry=executable_price,
                stop_loss=stop,
                target=tp,
                round_trip_cost_pct=cost_pct,
            )
        except (TypeError, ValueError):
            return False, "Unable to calculate live post-cost RR"

        if net_rr < max(MIN_RR, VALIDATOR_MIN_RR):
            return False, f"Live post-cost RR {net_rr:.2f} below minimum {max(MIN_RR, VALIDATOR_MIN_RR):.2f}"

        analysis["entry_drift_pct"] = drift
        analysis["trade_geometry_ok"] = True
        analysis["stop_distance_pct"] = risk / executable_price
        analysis["sl_atr"] = stop_atr
        analysis["tp_distance_atr"] = tp_atr
        analysis["tp_distance_pct"] = reward / executable_price
        analysis["rr_gross"] = rr
        analysis["rr_net"] = net_rr
        analysis["rr"] = net_rr
        analysis["live_geometry_reason"] = "OK"
        return True, "OK"

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
