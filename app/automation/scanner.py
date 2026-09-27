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


MEXC_INTERVALS = {
    "4H": "Hour4",
    "1H": "Min60",
    "15M": "Min15",
    "5M": "Min5",
    "1D": "Day1",
}


class MexcScanner:
    """
    Deterministic MEXC Futures scanner.

    Pipeline:

        MEXC DATA
            ↓
        ENGINE ANALYSIS
            ↓
        BTC FILTER
            ↓
        LIVE QUOTE
            ↓
        EXECUTION QUALITY
            ↓
        FUTURES CONTEXT
            ↓
        FINAL TECHNICAL SYNCHRONIZATION
            ↓
        SAFE LEVEL REPRICING
            ↓
        FINAL TECHNICAL SYNCHRONIZATION
            ↓
        SIGNAL VALIDATOR
            ↓
        PUBLISH / EXECUTE
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

        self._btc_context: dict[str, Any] = {
            "ok": False,
            "reason": "not loaded",
        }

    # =========================================================
    # PUBLIC SCAN
    # =========================================================

    async def scan_once(self) -> dict[str, int]:
        symbols = await self.universe.refresh()

        if not symbols:
            return {
                "symbols": 0,
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

        await self._refresh_btc_context()

        concurrency = max(
            1,
            int(
                getattr(
                    self.settings,
                    "scan_concurrency",
                    8,
                )
            ),
        )

        semaphore = asyncio.Semaphore(concurrency)

        results = await asyncio.gather(
            *(
                self._scan_one(
                    symbol,
                    semaphore,
                )
                for symbol in symbols
            ),
            return_exceptions=True,
        )

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

            if result.get("valid"):
                stats["valid"] += 1

            if result.get("sent"):
                stats["sent"] += 1

            if result.get("error"):
                stats["errors"] += 1

            stage = str(
                result.get(
                    "rejection_stage",
                    "",
                )
                or ""
            )

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
            "technical_candidates=%s rejected_setup=%s rejected_btc=%s "
            "rejected_futures=%s rejected_quote=%s rejected_freshness=%s "
            "rejected_execution_quality=%s rejected_final=%s",
            stats["symbols"],
            stats["valid"],
            stats["sent"],
            stats["errors"],
            stats["technical_candidates"],
            stats["rejected_setup"],
            stats["rejected_btc"],
            stats["rejected_futures"],
            stats["rejected_quote"],
            stats["rejected_freshness"],
            stats["rejected_execution_quality"],
            stats["rejected_final"],
        )

        return stats

    # =========================================================
    # BTC CONTEXT
    # =========================================================

    async def _refresh_btc_context(self) -> None:
        try:
            raw4, raw1, raw15 = await asyncio.gather(
                self.client.get_klines(
                    "BTC_USDT",
                    MEXC_INTERVALS["4H"],
                    250,
                ),
                self.client.get_klines(
                    "BTC_USDT",
                    MEXC_INTERVALS["1H"],
                    250,
                ),
                self.client.get_klines(
                    "BTC_USDT",
                    MEXC_INTERVALS["15M"],
                    250,
                ),
            )

            c4 = closed_candle_rows(
                raw4,
                "4h",
            )

            c1 = closed_candle_rows(
                raw1,
                "1h",
            )

            c15 = closed_candle_rows(
                raw15,
                "15m",
            )

            if len(c4) < 205:
                self._btc_context = {
                    "ok": False,
                    "reason": "insufficient BTC 4H history",
                }
                return

            if len(c1) < 205:
                self._btc_context = {
                    "ok": False,
                    "reason": "insufficient BTC 1H history",
                }
                return

            if len(c15) < 80:
                self._btc_context = {
                    "ok": False,
                    "reason": "insufficient BTC 15M history",
                }
                return

            self._btc_context = build_btc_context(
                c4,
                c1,
                c15,
            )

        except Exception as exc:
            LOGGER.warning(
                "BTC market context unavailable: %s",
                exc,
            )

            self._btc_context = {
                "ok": False,
                "reason": str(exc),
            }

    # =========================================================
    # SINGLE SYMBOL SCAN
    # =========================================================

    async def _scan_one(
        self,
        symbol: str,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:

        async with semaphore:
            try:
                limit = max(
                    250,
                    int(
                        getattr(
                            self.settings,
                            "candle_limit",
                            250,
                        )
                    ),
                )

                # -------------------------------------------------
                # MARKET DATA
                # -------------------------------------------------

                raw4, raw1, raw15, raw5 = await asyncio.gather(
                    self.client.get_klines(
                        symbol,
                        MEXC_INTERVALS["4H"],
                        limit,
                    ),
                    self.client.get_klines(
                        symbol,
                        MEXC_INTERVALS["1H"],
                        limit,
                    ),
                    self.client.get_klines(
                        symbol,
                        MEXC_INTERVALS["15M"],
                        limit,
                    ),
                    self.client.get_klines(
                        symbol,
                        MEXC_INTERVALS["5M"],
                        limit,
                    ),
                )

                c4 = closed_candle_rows(
                    raw4,
                    "4h",
                )

                c1 = closed_candle_rows(
                    raw1,
                    "1h",
                )

                c15 = closed_candle_rows(
                    raw15,
                    "15m",
                )

                c5 = closed_candle_rows(
                    raw5,
                    "5m",
                )

                required_history = (
                    (c4, 205, "4H"),
                    (c1, 205, "1H"),
                    (c15, 80, "15M"),
                    (c5, 30, "5M"),
                )

                for candles, minimum, label in required_history:
                    if len(candles) < minimum:
                        return self._reject(
                            symbol,
                            f"Insufficient closed {label} candles",
                            stage="DATA",
                            analysis={},
                        )

                # -------------------------------------------------
                # DAILY DATA
                # -------------------------------------------------

                raw1d = await self.client.get_klines(
                    symbol,
                    MEXC_INTERVALS["1D"],
                    60,
                )

                c1d = closed_candle_rows(
                    raw1d,
                    "1d",
                )

                # -------------------------------------------------
                # ENGINE
                # -------------------------------------------------

                analysis = analyze_candles(
                    symbol,
                    c4,
                    c1,
                    c15,
                    c5,
                    c1d,
                )

                if not isinstance(
                    analysis,
                    dict,
                ):
                    return self._reject(
                        symbol,
                        "Analysis engine returned invalid result",
                        stage="TECHNICAL",
                        analysis={},
                    )

                analysis.update(
                    {
                        "symbol": symbol,
                        "mexc_4h_rows": c4,
                        "mexc_1h_rows": c1,
                        "mexc_15m_rows": c15,
                        "mexc_5m_rows": c5,
                        "mexc_1d_rows": c1d,
                        "closed_4h_candles": len(c4),
                        "closed_1h_candles": len(c1),
                        "closed_15m_candles": len(c15),
                        "closed_5m_candles": len(c5),
                        "closed_5m_candle_time": int(
                            c5[-1]["time"]
                        ),
                    }
                )

                # -------------------------------------------------
                # INITIAL ENGINE DIAGNOSTICS
                # -------------------------------------------------

                self._synchronize_technical_diagnostics(
                    analysis
                )

                setup = str(
                    analysis.get("setup")
                    or "NO TRADE"
                ).upper()

                LOGGER.info(
                    "MEXC ANALYSIS | %s | setup=%s | score=%s | rr=%s | "
                    "stage=%s | failures=%s | bos(L/S)=%s/%s | "
                    "retest(L/S)=%s/%s | trigger=%s",
                    symbol,
                    setup,
                    analysis.get("score"),
                    analysis.get("rr"),
                    analysis.get(
                        "rejection_stage"
                    )
                    or "CANDIDATE",
                    analysis.get(
                        "technical_gate_failures",
                        [],
                    ),
                    analysis.get(
                        "long_bos_event_count",
                        0,
                    ),
                    analysis.get(
                        "short_bos_event_count",
                        0,
                    ),
                    analysis.get(
                        "long_retest",
                        False,
                    ),
                    analysis.get(
                        "short_retest",
                        False,
                    ),
                    analysis.get(
                        "trigger_5m",
                        "NONE",
                    ),
                )

                # -------------------------------------------------
                # NO TECHNICAL SETUP
                # -------------------------------------------------

                if setup not in {
                    "LONG",
                    "SHORT",
                }:
                    return self._reject(
                        symbol,
                        analysis.get(
                            "technical_gate_failures"
                        )
                        or "Analysis engine produced no valid LONG/SHORT setup",
                        stage="TECHNICAL",
                        analysis=analysis,
                    )

                # -------------------------------------------------
                # BTC FILTER
                # -------------------------------------------------

                btc_ok, btc_reason = btc_filter_ok(
                    setup,
                    self._btc_context,
                    is_btc=(
                        symbol.upper()
                        == "BTC_USDT"
                    ),
                )

                analysis["btc_filter_ok"] = bool(
                    btc_ok
                )

                analysis["btc_filter_reason"] = (
                    btc_reason
                )

                analysis["btc_context"] = (
                    self._btc_context
                )

                if not btc_ok:
                    return self._reject(
                        symbol,
                        btc_reason,
                        stage="BTC",
                        analysis=analysis,
                    )

                # -------------------------------------------------
                # LIVE TICKER
                # -------------------------------------------------

                ticker = await self.client.get_ticker(
                    symbol
                )

                if not ticker:
                    return self._reject(
                        symbol,
                        "Missing MEXC ticker",
                        stage="QUOTE",
                        analysis=analysis,
                    )

                now_ms = int(
                    time.time() * 1000
                )

                ts = self._safe_int(
                    ticker.get("timestamp")
                    or ticker.get("ts")
                    or ticker.get("time")
                )

                if 0 < ts < 10**12:
                    ts *= 1000

                max_age_ms = int(
                    float(
                        getattr(
                            self.settings,
                            "max_data_age_seconds",
                            5.0,
                        )
                    )
                    * 1000
                )

                data_fresh = bool(
                    ts > 0
                    and abs(
                        now_ms - ts
                    )
                    <= max_age_ms
                )

                analysis["ticker_timestamp"] = ts
                analysis["data_fresh"] = data_fresh

                if not data_fresh:
                    return self._reject(
                        symbol,
                        "MEXC ticker is stale",
                        stage="FRESHNESS",
                        analysis=analysis,
                    )

                bid = self._safe_float(
                    ticker.get("bid1")
                    or ticker.get("bidPrice")
                    or ticker.get("bid")
                )

                ask = self._safe_float(
                    ticker.get("ask1")
                    or ticker.get("askPrice")
                    or ticker.get("ask")
                )

                last = self._safe_float(
                    ticker.get("lastPrice")
                    or ticker.get("last")
                    or ticker.get("price")
                )

                if (
                    bid <= 0
                    or ask <= 0
                    or ask < bid
                ):
                    return self._reject(
                        symbol,
                        "Invalid MEXC bid/ask",
                        stage="QUOTE",
                        analysis=analysis,
                    )

                executable = (
                    ask
                    if setup == "LONG"
                    else bid
                )

                mid = (
                    bid + ask
                ) / 2.0

                spread = (
                    abs(
                        ask - bid
                    )
                    / mid
                    if mid > 0
                    else 999.0
                )

                analysis.update(
                    {
                        "mexc_bid": bid,
                        "mexc_ask": ask,
                        "mexc_last": last,
                        "mexc_spread_pct": spread,
                    }
                )

                max_spread = float(
                    getattr(
                        self.settings,
                        "max_mexc_spread_pct",
                        0.001,
                    )
                )

                analysis[
                    "max_mexc_spread_pct"
                ] = max_spread

                if spread > max_spread:
                    return self._reject(
                        symbol,
                        "MEXC spread too high",
                        stage="EXECUTION_QUALITY",
                        analysis=analysis,
                    )

                # -------------------------------------------------
                # INDEX / FAIR / FUNDING
                # -------------------------------------------------

                index_price = self._safe_float(
                    ticker.get("indexPrice")
                    or ticker.get("index")
                )

                fair_price = self._safe_float(
                    ticker.get("fairPrice")
                    or ticker.get("fair")
                    or ticker.get("markPrice")
                )

                funding = (
                    self._safe_float_or_none(
                        ticker.get("fundingRate")
                    )
                )

                if index_price <= 0:
                    index_data = await self._safe_call(
                        self.client.get_index_price,
                        symbol,
                    )

                    if index_data:
                        index_price = self._safe_float(
                            index_data.get(
                                "indexPrice"
                            )
                            or index_data.get(
                                "index"
                            )
                        )

                if fair_price <= 0:
                    fair_data = await self._safe_call(
                        self.client.get_fair_price,
                        symbol,
                    )

                    if fair_data:
                        fair_price = self._safe_float(
                            fair_data.get(
                                "fairPrice"
                            )
                            or fair_data.get(
                                "fair"
                            )
                        )

                if funding is None:
                    funding_data = await self._safe_call(
                        self.client.get_funding_rate,
                        symbol,
                    )

                    if funding_data:
                        funding = (
                            self._safe_float_or_none(
                                funding_data.get(
                                    "fundingRate"
                                )
                                or funding_data.get(
                                    "rate"
                                )
                            )
                        )

                reference = (
                    fair_price
                    if fair_price > 0
                    else index_price
                )

                if reference <= 0:
                    return self._reject(
                        symbol,
                        "Missing MEXC index/fair reference",
                        stage="EXECUTION_QUALITY",
                        analysis=analysis,
                    )

                dislocation = (
                    abs(
                        executable
                        - reference
                    )
                    / reference
                )

                max_dislocation = float(
                    getattr(
                        self.settings,
                        "max_index_dislocation_pct",
                        0.002,
                    )
                )

                analysis.update(
                    {
                        "mexc_index_price": index_price,
                        "mexc_fair_price": fair_price,
                        "mexc_funding_rate": funding,
                        "mexc_reference_dislocation_pct": dislocation,
                        "max_index_dislocation_pct": max_dislocation,
                    }
                )

                if dislocation > max_dislocation:
                    return self._reject(
                        symbol,
                        "MEXC executable price is too far from index/fair",
                        stage="EXECUTION_QUALITY",
                        analysis=analysis,
                    )

                # -------------------------------------------------
                # ORDER BOOK + TRADE FLOW
                # -------------------------------------------------

                depth, deals = await asyncio.gather(
                    self._safe_call(
                        self.client.get_depth,
                        symbol,
                        int(
                            getattr(
                                self.settings,
                                "orderbook_levels",
                                10,
                            )
                        ),
                    ),
                    self._safe_call(
                        self.client.get_deals,
                        symbol,
                        int(
                            getattr(
                                self.settings,
                                "trade_flow_limit",
                                100,
                            )
                        ),
                    ),
                )

                if depth:
                    analysis.update(
                        self._calculate_depth(
                            depth
                        )
                    )

                if deals:
                    analysis.update(
                        self._calculate_trade_flow(
                            deals
                        )
                    )

                analysis["hold_vol"] = (
                    self._safe_float(
                        ticker.get("holdVol")
                        or ticker.get("holdVolume")
                    )
                )

                analysis["funding_available"] = (
                    funding is not None
                )

                # -------------------------------------------------
                # FUTURES CONTEXT
                # -------------------------------------------------

                futures_ok = (
                    self._futures_context_ok(
                        analysis,
                        setup,
                    )
                )

                analysis["futures_ok"] = (
                    futures_ok
                )

                analysis["futures_context"] = (
                    "AVAILABLE"
                    if futures_ok
                    else "INSUFFICIENT_DIRECTIONAL_CONFIRMATION"
                )

                # -------------------------------------------------
                # CONFIRMATION FAMILIES + SCORE
                # -------------------------------------------------

                self._update_confirmation_families(
                    analysis
                )

                analysis["score"], analysis[
                    "score_groups"
                ] = self._recalculate_score(
                    analysis
                )

                # -------------------------------------------------
                # PLANNED ENTRY
                # -------------------------------------------------

                planned_entry = self._safe_float(
                    analysis.get("entry")
                )

                if planned_entry <= 0:
                    return self._reject(
                        symbol,
                        "Invalid planned entry",
                        stage="LEVELS",
                        analysis=analysis,
                    )

                # -------------------------------------------------
                # ENTRY DRIFT
                # -------------------------------------------------

                drift = (
                    abs(
                        executable
                        - planned_entry
                    )
                    / planned_entry
                )

                max_entry_drift = float(
                    getattr(
                        self.settings,
                        "max_entry_drift_pct",
                        0.002,
                    )
                )

                analysis["entry_drift_pct"] = (
                    drift
                )

                analysis["max_entry_drift_pct"] = (
                    max_entry_drift
                )

                if drift > max_entry_drift:
                    return self._reject(
                        symbol,
                        "Executable entry drift exceeds limit",
                        stage="EXECUTION_QUALITY",
                        analysis=analysis,
                    )

                # -------------------------------------------------
                # SAFE LEVEL REPRICING
                # -------------------------------------------------

                repriced, repricing_reason = (
                    self._reprice_levels(
                        analysis,
                        executable,
                    )
                )

                if not repriced:
                    return self._reject(
                        symbol,
                        repricing_reason,
                        stage="LEVELS",
                        analysis=analysis,
                    )

                # -------------------------------------------------
                # FINAL TECHNICAL SYNCHRONIZATION
                #
                # Repricing can change RR and therefore the final
                # technical candidate state.
                # -------------------------------------------------

                self._update_confirmation_families(
                    analysis
                )

                analysis["score"], analysis[
                    "score_groups"
                ] = self._recalculate_score(
                    analysis
                )

                self._synchronize_final_candidate(
                    analysis
                )

                # -------------------------------------------------
                # SIGNAL AGE CONFIGURATION
                # -------------------------------------------------

                analysis[
                    "max_signal_age_seconds"
                ] = float(
                    getattr(
                        self.settings,
                        "max_signal_age_seconds",
                        330.0,
                    )
                )

                # -------------------------------------------------
                # FINAL VALIDATOR
                # -------------------------------------------------

                validated, reasons = validate_signal(
                    analysis,
                    min_confluence=int(
                        getattr(
                            self.settings,
                            "min_confluence",
                            82,
                        )
                    ),
                    min_rr=float(
                        getattr(
                            self.settings,
                            "min_rr",
                            2.0,
                        )
                    ),
                    require_increasing_volume=bool(
                        getattr(
                            self.settings,
                            "require_increasing_volume",
                            False,
                        )
                    ),
                )

                if validated is None:
                    return self._reject(
                        symbol,
                        reasons,
                        stage="FINAL_VALIDATOR",
                        analysis=analysis,
                    )

                # -------------------------------------------------
                # PUBLISH
                # -------------------------------------------------

                sent = False

                if bool(
                    getattr(
                        self.settings,
                        "auto_signal_enabled",
                        False,
                    )
                ):
                    sent = await self._publish_signal(
                        validated
                    )

                # -------------------------------------------------
                # OPTIONAL LIVE EXECUTION
                # -------------------------------------------------

                if (
                    bool(
                        getattr(
                            self.settings,
                            "auto_trade_enabled",
                            False,
                        )
                    )
                    and bool(
                        getattr(
                            self.settings,
                            "allow_live_execution",
                            False,
                        )
                    )
                    and self.executor is not None
                ):
                    await self._execute_signal(
                        validated
                    )

                return {
                    "valid": True,
                    "sent": sent,
                    "error": False,
                    "symbol": symbol,
                    "analysis": analysis,
                    "signal": validated,
                    "technical_candidate": True,
                }

            except Exception as exc:
                LOGGER.exception(
                    "MEXC scan failed for %s",
                    symbol,
                )

                return {
                    "valid": False,
                    "sent": False,
                    "error": True,
                    "symbol": symbol,
                    "reason": str(exc),
                    "rejection_stage": "ERROR",
                    "technical_candidate": False,
                }

    # =========================================================
    # ENGINE DIAGNOSTIC SYNCHRONIZATION
    # =========================================================

    @staticmethod
    def _synchronize_technical_diagnostics(
        analysis: dict[str, Any],
    ) -> None:
        """
        Synchronize scanner diagnostics with the engine result.

        IMPORTANT:
        This method never creates a setup.

        It only translates the engine's existing state into
        explicit diagnostic failures.
        """

        setup = str(
            analysis.get("setup")
            or "NO TRADE"
        ).upper()

        failures_raw = analysis.get(
            "technical_gate_failures"
        )

        if isinstance(
            failures_raw,
            list,
        ):
            failures = [
                str(item).strip()
                for item in failures_raw
                if str(item).strip()
            ]
        else:
            failures = []

        # ---------------------------------------------------------
        # NO SETUP
        # ---------------------------------------------------------

        if setup not in {
            "LONG",
            "SHORT",
        }:
            direction_ok = bool(
                analysis.get(
                    "direction_ok",
                    False,
                )
            )

            structure_ok = bool(
                analysis.get(
                    "structure_ok",
                    False,
                )
            )

            setup_ok = bool(
                analysis.get(
                    "setup_ok",
                    False,
                )
            )

            momentum_ok = bool(
                analysis.get(
                    "momentum_ok",
                    False,
                )
            )

            volume_ok = bool(
                analysis.get(
                    "volume_ok",
                    False,
                )
            )

            location_ok = bool(
                analysis.get(
                    "location_ok",
                    False,
                )
            )

            volatility_ok = bool(
                analysis.get(
                    "volatility_ok",
                    False,
                )
            )

            risk_ok = bool(
                analysis.get(
                    "risk_ok",
                    False,
                )
            )

            trigger_side = str(
                analysis.get(
                    "trigger_side",
                    "NONE",
                )
                or "NONE"
            ).upper()

            trigger_state = str(
                analysis.get(
                    "trigger_5m",
                    "NONE",
                )
                or "NONE"
            ).upper()

            five_minute_ready = bool(
                analysis.get(
                    "five_minute_ready",
                    False,
                )
            )

            # -----------------------------------------------------
            # DIRECTION
            # -----------------------------------------------------

            if (
                not direction_ok
                and "1H alignment"
                not in failures
            ):
                failures.append(
                    "1H alignment"
                )

            # -----------------------------------------------------
            # 15M STRUCTURE
            # -----------------------------------------------------

            if (
                not structure_ok
                and "15M structure"
                not in failures
            ):
                failures.append(
                    "15M structure"
                )

            # -----------------------------------------------------
            # 5M TRIGGER
            #
            # DO NOT infer trigger failure merely because setup_ok
            # is false.
            #
            # If trigger_side is NONE because 1H/15M selection
            # failed, the correct diagnostic is the upstream gate.
            # -----------------------------------------------------

            trigger_failed = bool(
                (
                    trigger_side
                    in {
                        "LONG",
                        "SHORT",
                    }
                    and not five_minute_ready
                )
                or (
                    trigger_state
                    not in {
                        "NONE",
                        "",
                    }
                    and trigger_state
                    not in {
                        "LONG",
                        "SHORT",
                    }
                )
            )

            if (
                trigger_failed
                and "5M trigger"
                not in failures
            ):
                failures.append(
                    "5M trigger"
                )

            # -----------------------------------------------------
            # MOMENTUM
            # -----------------------------------------------------

            if (
                not momentum_ok
                and "momentum"
                not in failures
            ):
                failures.append(
                    "momentum"
                )

            # -----------------------------------------------------
            # VOLUME
            # -----------------------------------------------------

            if (
                not volume_ok
                and "volume"
                not in failures
            ):
                failures.append(
                    "volume"
                )

            # -----------------------------------------------------
            # LOCATION
            # -----------------------------------------------------

            if (
                not location_ok
                and "location/target path"
                not in failures
            ):
                failures.append(
                    "location/target path"
                )

            # -----------------------------------------------------
            # VOLATILITY
            # -----------------------------------------------------

            if (
                not volatility_ok
                and "volatility"
                not in failures
            ):
                failures.append(
                    "volatility"
                )

            # -----------------------------------------------------
            # RISK / RR
            # -----------------------------------------------------

            if (
                not risk_ok
                and "risk/RR"
                not in failures
            ):
                failures.append(
                    "risk/RR"
                )

            # -----------------------------------------------------
            # FINAL FALLBACK
            # -----------------------------------------------------

            if not failures:
                failures.append(
                    "No LONG/SHORT setup selected by engine"
                )

            analysis[
                "technical_candidate"
            ] = False

            analysis[
                "signal_blocked"
            ] = True

            analysis[
                "rejection_stage"
            ] = "TECHNICAL"

        else:
            # A setup exists, but the engine may still have technical
            # hard-gate failures. Do not create a candidate here.
            analysis[
                "technical_gate_failures"
            ] = failures

    # =========================================================
    # FINAL TECHNICAL CANDIDATE SYNCHRONIZATION
    # =========================================================

    @staticmethod
    def _synchronize_final_candidate(
        analysis: dict[str, Any],
    ) -> None:
        """
        Rebuild the final technical_candidate flag from the current
        post-repricing analysis.

        This prevents a stale candidate flag from surviving after
        RR, levels, score, or confirmation families change.
        """

        side = str(
            analysis.get("setup")
            or ""
        ).upper()

        score = MexcScanner._safe_int(
            analysis.get("score")
        )

        rr = MexcScanner._safe_float(
            analysis.get("rr")
        )

        min_score = max(
            82,
            MexcScanner._safe_int(
                analysis.get(
                    "min_confluence",
                    getattr(
                        analysis,
                        "min_confluence",
                        82,
                    ),
                )
            ),
        )

        # The engine's normal configuration is 82.
        # If the analysis does not explicitly carry a minimum,
        # use 82.
        if min_score <= 0:
            min_score = 82

        min_rr = MexcScanner._safe_float(
            analysis.get(
                "min_rr",
                2.0,
            )
        )

        if min_rr <= 0:
            min_rr = 2.0

        families = MexcScanner._safe_int(
            analysis.get(
                "confirmation_family_count",
                0,
            )
        )

        direction_ok = bool(
            analysis.get(
                "direction_ok",
                False,
            )
        )

        structure_ok = bool(
            analysis.get(
                "structure_ok",
                False,
            )
        )

        setup_ok = bool(
            analysis.get(
                "setup_ok",
                False,
            )
        )

        momentum_ok = bool(
            analysis.get(
                "momentum_ok",
                False,
            )
        )

        volume_ok = bool(
            analysis.get(
                "volume_ok",
                False,
            )
        )

        location_ok = bool(
            analysis.get(
                "location_ok",
                False,
            )
        )

        volatility_ok = bool(
            analysis.get(
                "volatility_ok",
                False,
            )
        )

        risk_ok = bool(
            analysis.get(
                "risk_ok",
                False,
            )
        )

        btc_ok = bool(
            analysis.get(
                "btc_filter_ok",
                False,
            )
        )

        data_fresh = bool(
            analysis.get(
                "data_fresh",
                False,
            )
        )

        structural_targets = bool(
            analysis.get(
                "target_path_structural",
                False,
            )
        )

        signal_blocked = bool(
            analysis.get(
                "signal_blocked",
                False,
            )
        )

        sl_atr = MexcScanner._safe_float(
            analysis.get(
                "sl_atr"
            )
        )

        rr_ok = rr >= min_rr

        score_ok = score >= min_score

        families_ok = (
            families >= 5
        )

        sl_ok = (
            sl_atr >= 0.50
            and sl_atr <= 1.80
        )

        candidate = bool(
            side in {
                "LONG",
                "SHORT",
            }
            and not signal_blocked
            and direction_ok
            and structure_ok
            and setup_ok
            and momentum_ok
            and volume_ok
            and location_ok
            and volatility_ok
            and risk_ok
            and btc_ok
            and data_fresh
            and structural_targets
            and score_ok
            and rr_ok
            and families_ok
            and sl_ok
        )

        analysis[
            "technical_candidate"
        ] = candidate

        # ---------------------------------------------------------
        # Keep diagnostics synchronized.
        # ---------------------------------------------------------

        failures_raw = analysis.get(
            "technical_gate_failures"
        )

        if isinstance(
            failures_raw,
            list,
        ):
            failures = [
                str(item).strip()
                for item in failures_raw
                if str(item).strip()
            ]
        else:
            failures = []

        def add_failure(
            condition: bool,
            message: str,
        ) -> None:
            if condition and message not in failures:
                failures.append(message)

        add_failure(
            not direction_ok,
            "4H/1H direction",
        )

        add_failure(
            not structure_ok,
            "15M structure",
        )

        add_failure(
            not setup_ok,
            "15M setup / 5M trigger",
        )

        add_failure(
            not momentum_ok,
            "momentum",
        )

        add_failure(
            not volume_ok,
            "volume",
        )

        add_failure(
            not location_ok,
            "location/target path",
        )

        add_failure(
            not volatility_ok,
            "volatility",
        )

        add_failure(
            not risk_ok,
            "risk/RR",
        )

        add_failure(
            not score_ok,
            "score",
        )

        add_failure(
            not families_ok,
            "confirmation families",
        )

        add_failure(
            not rr_ok,
            "risk/RR",
        )

        add_failure(
            not sl_ok,
            "SL distance",
        )

        add_failure(
            not btc_ok,
            "BTC/global filter",
        )

        add_failure(
            not data_fresh,
            "market data freshness",
        )

        add_failure(
            not structural_targets,
            "structural target path",
        )

        analysis[
            "technical_gate_failures"
        ] = failures

        if candidate:
            analysis[
                "signal_blocked"
            ] = False

    # =========================================================
    # FUTURES CONTEXT
    # =========================================================

    @staticmethod
    def _futures_context_ok(
        analysis: dict[str, Any],
        side: str,
    ) -> bool:
        """
        Return True only when live futures flow has directional
        agreement.

        Futures context is supporting context and is NOT an
        independent technical hard gate.
        """

        imbalance = MexcScanner._safe_float(
            analysis.get(
                "orderbook_imbalance",
                0.0,
            )
        )

        flow = MexcScanner._safe_float(
            analysis.get(
                "volume_delta_ratio",
                0.0,
            )
        )

        threshold = 0.05

        if side == "LONG":
            return bool(
                imbalance >= threshold
                or flow >= threshold
            )

        if side == "SHORT":
            return bool(
                imbalance <= -threshold
                or flow <= -threshold
            )

        return False

    # =========================================================
    # CONFIRMATION FAMILIES
    # =========================================================

    @staticmethod
    def _update_confirmation_families(
        analysis: dict[str, Any],
    ) -> None:
        families = (
            "direction_ok",
            "structure_ok",
            "setup_ok",
            "momentum_ok",
            "volume_ok",
            "location_ok",
        )

        analysis[
            "confirmation_family_count"
        ] = sum(
            bool(
                analysis.get(
                    name,
                    False,
                )
            )
            for name in families
        )

    # =========================================================
    # SCORE
    # =========================================================

    @staticmethod
    def _recalculate_score(
        analysis: dict[str, Any],
    ) -> tuple[int, dict[str, int]]:

        groups = {
            "direction_regime": (
                20
                if analysis.get("direction_ok")
                else 0
            ),
            "market_structure": (
                20
                if analysis.get("structure_ok")
                else 0
            ),
            "setup_entry_trigger": (
                20
                if analysis.get("setup_ok")
                else 0
            ),
            "momentum": (
                10
                if analysis.get("momentum_ok")
                else 0
            ),
            "volume_participation": (
                10
                if analysis.get("volume_ok")
                else 0
            ),
            "location_target_path": (
                10
                if analysis.get("location_ok")
                else 0
            ),
            "futures_market_context": (
                5
                if analysis.get("futures_ok")
                else 0
            ),
            "volatility_execution": (
                5
                if analysis.get("volatility_ok")
                else 0
            ),
        }

        setup_q = MexcScanner._safe_float(
            analysis.get(
                "trigger_quality_5m",
                0.0,
            )
        )

        bos_q = MexcScanner._safe_float(
            analysis.get(
                "bos_15m_strength",
                0.0,
            )
        )

        retest = (
            analysis.get("retest")
            or {}
        )

        if not isinstance(
            retest,
            dict,
        ):
            retest = {}

        ret_q = MexcScanner._safe_float(
            retest.get(
                "quality",
                0.0,
            )
        )

        quality = (
            0.50 * setup_q
            + 0.25 * bos_q
            + 0.25 * ret_q
        )

        if (
            groups["setup_entry_trigger"]
            and quality < 0.60
        ):
            groups[
                "setup_entry_trigger"
            ] -= 5

        rvol_15m = MexcScanner._safe_float(
            analysis.get(
                "rvol_15m",
                0.0,
            )
        )

        if (
            groups["volume_participation"]
            and rvol_15m < 1.25
        ):
            groups[
                "volume_participation"
            ] -= 2

        score = sum(
            groups.values()
        )

        return (
            max(
                0,
                min(
                    100,
                    int(score),
                ),
            ),
            groups,
        )

    # =========================================================
    # SAFE NUMERIC HELPERS
    # =========================================================

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> float:
        try:
            result = (
                float(value)
                if value is not None
                else 0.0
            )

            if result != result:
                return 0.0

            return result

        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    @staticmethod
    def _safe_float_or_none(
        value: Any,
    ) -> float | None:
        try:
            result = float(value)

            if result != result:
                return None

            return result

        except (
            TypeError,
            ValueError,
        ):
            return None

    @staticmethod
    def _safe_int(
        value: Any,
    ) -> int:
        try:
            return (
                int(float(value))
                if value is not None
                else 0
            )

        except (
            TypeError,
            ValueError,
        ):
            return 0

    # =========================================================
    # SAFE OPTIONAL API CALL
    # =========================================================

    @staticmethod
    async def _safe_call(
        fn,
        *args,
    ):
        try:
            return await fn(*args)

        except Exception as exc:
            LOGGER.debug(
                "Optional MEXC context call failed: %s",
                exc,
            )
            return None

    # =========================================================
    # ORDER BOOK
    # =========================================================

    @staticmethod
    def _calculate_depth(
        orderbook: dict[str, Any],
    ) -> dict[str, float]:

        bids = (
            orderbook.get("bids")
            or []
        )

        asks = (
            orderbook.get("asks")
            or []
        )

        def total(
            levels: list[Any],
        ) -> float:

            value = 0.0

            for level in levels:
                try:
                    if isinstance(
                        level,
                        dict,
                    ):
                        qty = (
                            level.get(
                                "quantity"
                            )
                            or level.get(
                                "qty"
                            )
                            or level.get(
                                "volume"
                            )
                            or level.get(
                                "v"
                            )
                            or 0
                        )

                    else:
                        qty = (
                            level[1]
                            if len(level) > 1
                            else 0
                        )

                    value += float(
                        qty
                    )

                except Exception:
                    continue

            return value

        bid_depth = total(
            bids
        )

        ask_depth = total(
            asks
        )

        total_depth = (
            bid_depth
            + ask_depth
        )

        imbalance = (
            (
                bid_depth
                - ask_depth
            )
            / total_depth
            if total_depth > 0
            else 0.0
        )

        return {
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "orderbook_imbalance": imbalance,
        }

    # =========================================================
    # TRADE FLOW
    # =========================================================

    @staticmethod
    def _calculate_trade_flow(
        deals: list[Any],
    ) -> dict[str, float]:

        buy = 0.0
        sell = 0.0

        for deal in deals:
            if not isinstance(
                deal,
                dict,
            ):
                continue

            try:
                qty = float(
                    deal.get("v")
                    or deal.get("volume")
                    or deal.get("vol")
                    or deal.get("quantity")
                    or 0
                )

                side = deal.get(
                    "T",
                    deal.get(
                        "side",
                        deal.get(
                            "type",
                            "",
                        ),
                    ),
                )

                side_text = str(
                    side
                ).lower()

                if side_text in {
                    "1",
                    "buy",
                    "purchase",
                    "bid",
                }:
                    buy += qty

                elif side_text in {
                    "2",
                    "sell",
                    "ask",
                }:
                    sell += qty

            except Exception:
                continue

        total = (
            buy
            + sell
        )

        delta = (
            buy
            - sell
        )

        ratio = (
            delta / total
            if total > 0
            else 0.0
        )

        return {
            "buy_volume": buy,
            "sell_volume": sell,
            "volume_delta": delta,
            "volume_delta_ratio": ratio,
        }

    # =========================================================
    # SAFE LEVEL REPRICING
    # =========================================================

    @staticmethod
    def _reprice_levels(
        analysis: dict[str, Any],
        executable_price: float,
    ) -> tuple[bool, str]:

        # ---------------------------------------------------------
        # EXECUTABLE PRICE
        # ---------------------------------------------------------

        if (
            executable_price <= 0
            or executable_price != executable_price
        ):
            return (
                False,
                "Invalid executable price",
            )

        # ---------------------------------------------------------
        # REQUIRED LEVELS
        # ---------------------------------------------------------

        required_fields = (
            "entry",
            "stop_loss",
            "tp1",
            "tp2",
        )

        values: dict[str, float] = {}

        for field in required_fields:
            raw_value = analysis.get(
                field
            )

            if raw_value is None:
                return (
                    False,
                    f"Missing trade level: {field}",
                )

            try:
                value = float(
                    raw_value
                )

            except (
                TypeError,
                ValueError,
            ):
                return (
                    False,
                    f"Invalid trade level: {field}",
                )

            if (
                value <= 0
                or value != value
            ):
                return (
                    False,
                    f"Trade level must be positive: {field}",
                )

            values[field] = value

        # ---------------------------------------------------------
        # SIDE
        # ---------------------------------------------------------

        side = str(
            analysis.get("setup")
            or analysis.get("side")
            or ""
        ).upper()

        if side not in {
            "LONG",
            "SHORT",
        }:
            return (
                False,
                "Missing or invalid trade side for repricing",
            )

        old_entry = values[
            "entry"
        ]

        if old_entry <= 0:
            return (
                False,
                "Invalid original entry",
            )

        # ---------------------------------------------------------
        # TRANSLATION
        # ---------------------------------------------------------

        delta = (
            executable_price
            - old_entry
        )

        new_stop = (
            values["stop_loss"]
            + delta
        )

        new_tp1 = (
            values["tp1"]
            + delta
        )

        new_tp2 = (
            values["tp2"]
            + delta
        )

        # ---------------------------------------------------------
        # SIDE-SPECIFIC VALIDATION
        # ---------------------------------------------------------

        if side == "LONG":

            if not (
                new_stop
                < executable_price
                < new_tp1
                < new_tp2
            ):
                return (
                    False,
                    "Repriced LONG levels are invalid: "
                    "SL < Entry < TP1 < TP2 required",
                )

            risk = (
                executable_price
                - new_stop
            )

            reward = (
                new_tp2
                - executable_price
            )

        else:

            if not (
                new_tp2
                < new_tp1
                < executable_price
                < new_stop
            ):
                return (
                    False,
                    "Repriced SHORT levels are invalid: "
                    "TP2 < TP1 < Entry < SL required",
                )

            risk = (
                new_stop
                - executable_price
            )

            reward = (
                executable_price
                - new_tp2
            )

        if risk <= 0:
            return (
                False,
                "Repriced stop-loss produces zero risk",
            )

        if reward <= 0:
            return (
                False,
                "Repriced TP2 produces zero reward",
            )

        rr = (
            reward
            / risk
        )

        if (
            rr <= 0
            or rr != rr
        ):
            return (
                False,
                "Repriced RR is invalid",
            )

        # ---------------------------------------------------------
        # WRITE ONLY AFTER VALIDATION
        # ---------------------------------------------------------

        analysis["entry"] = float(
            executable_price
        )

        analysis["stop_loss"] = float(
            new_stop
        )

        analysis["tp1"] = float(
            new_tp1
        )

        analysis["tp2"] = float(
            new_tp2
        )

        analysis["rr"] = float(
            rr
        )

        analysis["repriced"] = True

        analysis[
            "repriced_from_entry"
        ] = float(
            old_entry
        )

        analysis[
            "repriced_entry"
        ] = float(
            executable_price
        )

        return (
            True,
            "OK",
        )

    # =========================================================
    # REJECTION
    # =========================================================

    @staticmethod
    def _reject(
        symbol: str,
        reason: Any,
        *,
        stage: str,
        analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:

        payload = (
            analysis
            if analysis is not None
            else {}
        )

        payload[
            "rejection_stage"
        ] = stage

        if isinstance(
            reason,
            list,
        ):
            payload[
                "rejection_reasons"
            ] = [
                str(item)
                for item in reason
                if str(item).strip()
            ]

        else:
            payload[
                "rejection_reasons"
            ] = [
                str(reason)
            ]

        LOGGER.info(
            "MEXC REJECT | %s | stage=%s | reason=%s | "
            "setup=%s | score=%s | rr=%s",
            symbol,
            stage,
            payload[
                "rejection_reasons"
            ],
            payload.get(
                "setup",
                "NO TRADE",
            ),
            payload.get(
                "score"
            ),
            payload.get(
                "rr"
            ),
        )

        return {
            "valid": False,
            "sent": False,
            "error": False,
            "symbol": symbol,
            "reason": reason,
            "analysis": payload,
            "rejection_stage": stage,
            "technical_candidate": bool(
                payload.get(
                    "technical_candidate",
                    False,
                )
            ),
        }

    # =========================================================
    # SIGNAL PUBLISH
    # =========================================================

    async def _publish_signal(
        self,
        signal,
    ):
        try:
            return bool(
                await self.signal_manager.publish(
                    signal
                )
            )

        except Exception:
            LOGGER.exception(
                "Signal publication failed for %s",
                signal.symbol,
            )

            return False

    # =========================================================
    # LIVE EXECUTION
    # =========================================================

    async def _execute_signal(
        self,
        signal,
    ):
        if self.executor is None:
            return None

        meta = (
            self.universe.get(
                signal.symbol
            )
            if hasattr(
                self.universe,
                "get",
            )
            else None
        )

        if meta is None:
            return None

        return await self.executor.execute(
            signal,
            meta,
        )
