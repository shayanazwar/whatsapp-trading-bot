from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..analysis.engine import (
    analyze_candles,
    closed_candle_rows,
)
from .mexc_client import MexcClient
from .signal_manager import SignalManager
from .signal_validator import validate_signal
from .universe import UniverseSelector

LOGGER = logging.getLogger(__name__)


MEXC_INTERVALS = {
    "4H": "Hour4",
    "1H": "Min60",
    "15M": "Min15",
    "5M": "Min5",
}


class MexcScanner:
    def __init__(
        self,
        *,
        settings: Any,
        client: MexcClient,
        universe: UniverseSelector,
        signal_manager: SignalManager,
    ) -> None:
        self.settings = settings
        self.client = client
        self.universe = universe
        self.signal_manager = signal_manager

    # ============================================================
    # PUBLIC SCAN
    # ============================================================

    async def scan_once(self) -> dict[str, int]:

        symbols = await self.universe.refresh()

        if not symbols:
            return {
                "symbols": 0,
                "valid": 0,
                "sent": 0,
                "errors": 0,
            }

        concurrency = max(
            1,
            int(
                getattr(
                    self.settings,
                    "scanner_concurrency",
                    5,
                )
            ),
        )

        semaphore = asyncio.Semaphore(
            concurrency
        )

        results = await asyncio.gather(
            *[
                self._scan_one(
                    symbol,
                    semaphore,
                )
                for symbol in symbols
            ],
            return_exceptions=True,
        )

        stats = {
            "symbols": len(symbols),
            "valid": 0,
            "sent": 0,
            "errors": 0,
        }

        for result in results:

            if isinstance(
                result,
                Exception,
            ):
                stats["errors"] += 1
                continue

            if not isinstance(
                result,
                dict,
            ):
                continue

            if result.get("valid"):
                stats["valid"] += 1

            if result.get("sent"):
                stats["sent"] += 1

            if result.get("error"):
                stats["errors"] += 1

        LOGGER.info(
            "MEXC scan complete: "
            "symbols=%s valid=%s sent=%s errors=%s",
            stats["symbols"],
            stats["valid"],
            stats["sent"],
            stats["errors"],
        )

        return stats

    # ============================================================
    # SYMBOL SCAN
    # ============================================================

    async def _scan_one(
        self,
        symbol: str,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:

        async with semaphore:

            try:

                # ====================================================
                # 1. FETCH 4H / 1H / 15M / 5M
                # ====================================================

                (
                    candles_4h_raw,
                    candles_1h_raw,
                    candles_15m_raw,
                    candles_5m_raw,
                ) = await asyncio.gather(

                    self.client.get_klines(
                        symbol,
                        MEXC_INTERVALS["4H"],
                        250,
                    ),

                    self.client.get_klines(
                        symbol,
                        MEXC_INTERVALS["1H"],
                        250,
                    ),

                    self.client.get_klines(
                        symbol,
                        MEXC_INTERVALS["15M"],
                        250,
                    ),

                    self.client.get_klines(
                        symbol,
                        MEXC_INTERVALS["5M"],
                        250,
                    ),
                )

                # ====================================================
                # 2. CLOSED CANDLES ONLY
                # ====================================================

                closed_4h = closed_candle_rows(
                    candles_4h_raw,
                    "4h",
                )

                closed_1h = closed_candle_rows(
                    candles_1h_raw,
                    "1h",
                )

                closed_15m = closed_candle_rows(
                    candles_15m_raw,
                    "15m",
                )

                closed_5m = closed_candle_rows(
                    candles_5m_raw,
                    "5m",
                )

                # ====================================================
                # 3. DATA SUFFICIENCY
                # ====================================================

                required = (
                    (closed_4h, 205, "4H"),
                    (closed_1h, 205, "1H"),
                    (closed_15m, 80, "15M"),
                    (closed_5m, 30, "5M"),
                )

                for candles, minimum, label in required:

                    if len(candles) < minimum:

                        return self._reject(
                            symbol,
                            f"Insufficient closed {label} candles",
                        )

                # ====================================================
                # 4. FULL MULTI-TIMEFRAME ENGINE
                # ====================================================

                analysis = analyze_candles(
                    symbol,
                    closed_4h,
                    closed_1h,
                    closed_15m,
                    closed_5m,
                )

                # ====================================================
                # 5. STORE 5M INFORMATION
                # ====================================================

                analysis[
                    "mexc_5m_rows"
                ] = closed_5m

                analysis[
                    "closed_5m_candles"
                ] = len(closed_5m)

                analysis[
                    "closed_5m_candle_time"
                ] = int(
                    closed_5m[-1][0]
                )

                # ====================================================
                # 6. NO SETUP = STOP
                # ====================================================

                setup = str(
                    analysis.get(
                        "setup",
                        "",
                    )
                ).upper()

                if setup not in {
                    "LONG",
                    "SHORT",
                }:

                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": "NO TRADE",
                        "analysis": analysis,
                    }

                # ====================================================
                # 7. FRESH EXECUTABLE MEXC QUOTE
                # ====================================================

                ticker = await self.client.get_ticker(
                    symbol
                )

                if not ticker:

                    return self._reject(
                        symbol,
                        "Missing MEXC ticker",
                    )

                bid = self._safe_float(
                    ticker.get("bidPrice")
                    or ticker.get("bid")
                )

                ask = self._safe_float(
                    ticker.get("askPrice")
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
                    )

                executable_price = (
                    ask
                    if setup == "LONG"
                    else bid
                )

                # ====================================================
                # 8. SPREAD
                # ====================================================

                mid = (
                    bid + ask
                ) / 2.0

                spread_pct = (
                    abs(ask - bid)
                    / mid
                    if mid > 0
                    else 999.0
                )

                max_spread_pct = float(
                    getattr(
                        self.settings,
                        "max_spread_pct",
                        0.002,
                    )
                )

                if (
                    spread_pct
                    > max_spread_pct
                ):

                    return self._reject(
                        symbol,
                        (
                            "Spread too high: "
                            f"{spread_pct:.6f}"
                        ),
                    )

                analysis.update(
                    {
                        "mexc_bid": bid,
                        "mexc_ask": ask,
                        "mexc_last": last,
                        "mexc_spread_pct": spread_pct,
                    }
                )

                # ====================================================
                # 9. INDEX / FAIR PRICE
                # ====================================================

                index_price = (
                    await self._safe_index_price(
                        symbol
                    )
                )

                fair_price = (
                    await self._safe_fair_price(
                        symbol
                    )
                )

                if index_price > 0:

                    analysis[
                        "mexc_index_price"
                    ] = index_price

                if fair_price > 0:

                    analysis[
                        "mexc_fair_price"
                    ] = fair_price

                reference_price = (
                    fair_price
                    if fair_price > 0
                    else index_price
                )

                if reference_price > 0:

                    dislocation_pct = (
                        abs(
                            executable_price
                            - reference_price
                        )
                        / reference_price
                    )

                    analysis[
                        "mexc_reference_dislocation_pct"
                    ] = dislocation_pct

                    max_dislocation = float(
                        getattr(
                            self.settings,
                            "max_price_dislocation_pct",
                            0.005,
                        )
                    )

                    if (
                        dislocation_pct
                        > max_dislocation
                    ):

                        return self._reject(
                            symbol,
                            "Executable price too far from MEXC reference",
                            analysis,
                        )

                # ====================================================
                # 10. FUNDING
                # ====================================================

                funding = (
                    await self._safe_funding(
                        symbol
                    )
                )

                if funding is not None:

                    analysis[
                        "mexc_funding_rate"
                    ] = funding

                # ====================================================
                # 11. ORDERBOOK
                # ====================================================

                orderbook = (
                    await self._safe_orderbook(
                        symbol
                    )
                )

                if orderbook:

                    depth = (
                        self._calculate_depth(
                            orderbook
                        )
                    )

                    analysis.update(
                        depth
                    )

                # ====================================================
                # 12. RECENT DEALS
                # ====================================================

                deals = (
                    await self._safe_deals(
                        symbol
                    )
                )

                if deals:

                    flow = (
                        self._calculate_trade_flow(
                            deals
                        )
                    )

                    analysis.update(
                        flow
                    )

                # ====================================================
                # 13. REAL FUTURES CONTEXT
                # ====================================================

                futures_ok = (
                    self._futures_context_ok(
                        analysis,
                        setup,
                    )
                )

                analysis[
                    "futures_ok"
                ] = futures_ok

                analysis[
                    "futures_context"
                ] = (
                    "AVAILABLE"
                    if futures_ok
                    else "FAILED"
                )

                if not futures_ok:

                    return self._reject(
                        symbol,
                        "MEXC futures context failed",
                        analysis,
                    )

                # ====================================================
                # 14. RECALCULATE SCORE
                #
                # Engine initially calculates the structural score.
                # Futures data was unavailable there.
                #
                # Now calculate the final score with REAL context.
                # ====================================================

                analysis[
                    "score"
                ], analysis[
                    "score_groups"
                ] = self._recalculate_score(
                    analysis
                )

                # ====================================================
                # 15. FINAL ENTRY DRIFT
                # ====================================================

                planned_entry = (
                    self._safe_float(
                        analysis.get(
                            "entry"
                        )
                    )
                )

                if planned_entry <= 0:

                    return self._reject(
                        symbol,
                        "Invalid planned entry",
                        analysis,
                    )

                entry_drift_pct = (
                    abs(
                        executable_price
                        - planned_entry
                    )
                    / planned_entry
                )

                analysis[
                    "entry_drift_pct"
                ] = entry_drift_pct

                max_entry_drift = float(
                    getattr(
                        self.settings,
                        "max_entry_drift_pct",
                        0.005,
                    )
                )

                if (
                    entry_drift_pct
                    > max_entry_drift
                ):

                    return self._reject(
                        symbol,
                        (
                            "Executable entry drift "
                            "exceeds limit"
                        ),
                        analysis,
                    )

                # ====================================================
                # 16. REPRICE AROUND REAL EXECUTION PRICE
                # ====================================================

                self._reprice_levels(
                    analysis,
                    executable_price,
                )

                # ====================================================
                # 17. RECHECK SCORE/RR AFTER REPRICE
                # ====================================================

                analysis[
                    "score"
                ], analysis[
                    "score_groups"
                ] = self._recalculate_score(
                    analysis
                )

                # ====================================================
                # 18. FINAL VALIDATOR
                # ====================================================

                validated, reasons = (
                    validate_signal(
                        analysis,
                        min_confluence=int(
                            getattr(
                                self.settings,
                                "min_confluence",
                                5,
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
                )

                if validated is None:

                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": reasons,
                        "analysis": analysis,
                    }

                # ====================================================
                # 19. PUBLISH
                # ====================================================

                sent = False

                auto_signals = bool(
                    getattr(
                        self.settings,
                        "auto_signals_enabled",
                        False,
                    )
                )

                if auto_signals:

                    sent = (
                        await self._publish_signal(
                            validated
                        )
                    )

                # ====================================================
                # 20. LIVE EXECUTION
                #
                # STILL DISABLED.
                # ====================================================

                auto_trade = bool(
                    getattr(
                        self.settings,
                        "auto_trade_enabled",
                        False,
                    )
                )

                live_allowed = bool(
                    getattr(
                        self.settings,
                        "live_execution_allowed",
                        False,
                    )
                )

                live_implemented = bool(
                    getattr(
                        self.settings,
                        "live_implemented",
                        False,
                    )
                )

                if (
                    auto_trade
                    and live_allowed
                    and live_implemented
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
                }

    # ============================================================
    # FUTURES CONTEXT
    # ============================================================

    @staticmethod
    def _futures_context_ok(
        analysis: dict[str, Any],
        side: str,
    ) -> bool:

        # We require actual market-context data.
        has_funding = (
            "mexc_funding_rate"
            in analysis
        )

        has_orderbook = (
            "orderbook_imbalance"
            in analysis
        )

        has_flow = (
            "volume_delta_ratio"
            in analysis
        )

        if not (
            has_funding
            or has_orderbook
            or has_flow
        ):
            return False

        confirmations = 0

        # --------------------------------------------------------
        # Orderbook
        # --------------------------------------------------------

        imbalance = float(
            analysis.get(
                "orderbook_imbalance",
                0.0,
            )
            or 0.0
        )

        if side == "LONG" and imbalance > 0:
            confirmations += 1

        if side == "SHORT" and imbalance < 0:
            confirmations += 1

        # --------------------------------------------------------
        # Trade flow
        # --------------------------------------------------------

        delta_ratio = float(
            analysis.get(
                "volume_delta_ratio",
                0.0,
            )
            or 0.0
        )

        if side == "LONG" and delta_ratio > 0:
            confirmations += 1

        if side == "SHORT" and delta_ratio < 0:
            confirmations += 1

        # --------------------------------------------------------
        # Funding
        #
        # Funding is NOT treated as a standalone direction signal.
        # It only acts as a context/warning input.
        # --------------------------------------------------------

        if has_funding:
            confirmations += 1

        return confirmations >= 2

    # ============================================================
    # SCORE
    # ============================================================

    @staticmethod
    def _recalculate_score(
        analysis: dict[str, Any],
    ) -> tuple[int, dict[str, int]]:

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

        futures_ok = bool(
            analysis.get(
                "futures_ok",
                False,
            )
        )

        volatility_ok = bool(
            analysis.get(
                "volatility_ok",
                False,
            )
        )

        groups = {
            "direction_regime": (
                20 if direction_ok else 0
            ),

            "market_structure": (
                20 if structure_ok else 0
            ),

            "setup_trigger": (
                20 if setup_ok else 0
            ),

            "momentum": (
                10 if momentum_ok else 0
            ),

            "volume_participation": (
                10 if volume_ok else 0
            ),

            "location_target_path": (
                10 if location_ok else 0
            ),

            "futures_context": (
                5 if futures_ok else 0
            ),

            "volatility_execution": (
                5 if volatility_ok else 0
            ),
        }

        return (
            sum(groups.values()),
            groups,
        )

    # ============================================================
    # SAFE FLOAT
    # ============================================================

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> float:

        try:

            if value is None:
                return 0.0

            return float(value)

        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    # ============================================================
    # SAFE FUNDING
    # ============================================================

    async def _safe_funding(
        self,
        symbol: str,
    ) -> float | None:

        try:

            result = await self.client.get_funding_rate(
                symbol
            )

            if not isinstance(
                result,
                dict,
            ):
                return None

            value = (
                result.get(
                    "fundingRate"
                )
                or result.get(
                    "funding_rate"
                )
                or result.get(
                    "rate"
                )
            )

            if value is None:
                return None

            return float(value)

        except Exception:

            return None

    # ============================================================
    # SAFE INDEX
    # ============================================================

    async def _safe_index_price(
        self,
        symbol: str,
    ) -> float:

        try:

            result = (
                await self.client.get_index_price(
                    symbol
                )
            )

            if not isinstance(
                result,
                dict,
            ):
                return 0.0

            return self._safe_float(
                result.get(
                    "indexPrice"
                )
                or result.get(
                    "index"
                )
                or result.get(
                    "price"
                )
            )

        except Exception:

            return 0.0

    # ============================================================
    # SAFE FAIR PRICE
    # ============================================================

    async def _safe_fair_price(
        self,
        symbol: str,
    ) -> float:

        try:

            result = (
                await self.client.get_fair_price(
                    symbol
                )
            )

            if not isinstance(
                result,
                dict,
            ):
                return 0.0

            return self._safe_float(
                result.get(
                    "fairPrice"
                )
                or result.get(
                    "fair"
                )
                or result.get(
                    "markPrice"
                )
                or result.get(
                    "mark"
                )
                or result.get(
                    "price"
                )
            )

        except Exception:

            return 0.0

    # ============================================================
    # SAFE ORDERBOOK
    # ============================================================

    async def _safe_orderbook(
        self,
        symbol: str,
    ) -> dict[str, Any] | None:

        try:

            result = await self.client.get_depth(
                symbol,
                20,
            )

            if isinstance(
                result,
                dict,
            ):
                return result

            return None

        except Exception:

            return None

    # ============================================================
    # SAFE DEALS
    # ============================================================

    async def _safe_deals(
        self,
        symbol: str,
    ) -> list[dict[str, Any]] | None:

        try:

            result = await self.client.get_deals(
                symbol,
                100,
            )

            if isinstance(
                result,
                list,
            ):
                return [
                    item
                    for item in result
                    if isinstance(
                        item,
                        dict,
                    )
                ]

            return None

        except Exception:

            return None

    # ============================================================
    # ORDERBOOK DEPTH
    # ============================================================

    def _calculate_depth(
        self,
        orderbook: dict[str, Any],
    ) -> dict[str, Any]:

        bids = (
            orderbook.get("bids")
            or []
        )

        asks = (
            orderbook.get("asks")
            or []
        )

        def total_volume(
            levels: list[Any],
        ) -> float:

            total = 0.0

            for level in levels:

                try:

                    if isinstance(
                        level,
                        dict,
                    ):

                        quantity = (
                            level.get(
                                "quantity"
                            )
                            or level.get(
                                "qty"
                            )
                            or level.get(
                                "volume"
                            )
                            or 0
                        )

                    else:

                        quantity = (
                            level[1]
                            if len(level) > 1
                            else 0
                        )

                    total += float(
                        quantity
                    )

                except Exception:

                    continue

            return total

        bid_depth = (
            total_volume(
                bids
            )
        )

        ask_depth = (
            total_volume(
                asks
            )
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

    # ============================================================
    # TRADE FLOW
    # ============================================================

    def _calculate_trade_flow(
        self,
        deals: list[dict[str, Any]],
    ) -> dict[str, Any]:

        buy_volume = 0.0
        sell_volume = 0.0

        for deal in deals:

            try:

                quantity = (
                    deal.get(
                        "quantity"
                    )
                    or deal.get(
                        "qty"
                    )
                    or deal.get(
                        "volume"
                    )
                    or deal.get(
                        "vol"
                    )
                    or 0
                )

                quantity = float(
                    quantity
                )

                side = str(
                    deal.get(
                        "side"
                    )
                    or deal.get(
                        "type"
                    )
                    or ""
                ).lower()

                # MEXC trade-side formats vary.
                if (
                    "buy" in side
                    or side in {
                        "1",
                        "bid",
                    }
                ):

                    buy_volume += quantity

                elif (
                    "sell" in side
                    or side in {
                        "2",
                        "ask",
                    }
                ):

                    sell_volume += quantity

            except Exception:

                continue

        total = (
            buy_volume
            + sell_volume
        )

        delta = (
            buy_volume
            - sell_volume
        )

        delta_ratio = (
            delta / total
            if total > 0
            else 0.0
        )

        return {
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "volume_delta": delta,
            "volume_delta_ratio": delta_ratio,
        }

    # ============================================================
    # REPRICE
    # ============================================================

    @staticmethod
    def _reprice_levels(
        analysis: dict[str, Any],
        executable_price: float,
    ) -> None:

        old_entry = float(
            analysis["entry"]
        )

        old_sl = float(
            analysis["stop_loss"]
        )

        old_tp1 = float(
            analysis["tp1"]
        )

        old_tp2 = float(
            analysis["tp2"]
        )

        delta = (
            executable_price
            - old_entry
        )

        new_sl = (
            old_sl + delta
        )

        new_tp1 = (
            old_tp1 + delta
        )

        new_tp2 = (
            old_tp2 + delta
        )

        analysis[
            "entry"
        ] = executable_price

        analysis[
            "stop_loss"
        ] = new_sl

        analysis[
            "tp1"
        ] = new_tp1

        analysis[
            "tp2"
        ] = new_tp2

        risk = abs(
            executable_price
            - new_sl
        )

        reward = abs(
            new_tp2
            - executable_price
        )

        analysis[
            "rr"
        ] = (
            reward / risk
            if risk > 0
            else 0.0
        )

    # ============================================================
    # REJECTION
    # ============================================================

    @staticmethod
    def _reject(
        symbol: str,
        reason: str,
        analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:

        return {
            "valid": False,
            "sent": False,
            "error": False,
            "symbol": symbol,
            "reason": reason,
            "analysis": analysis,
        }

    # ============================================================
    # PUBLISH
    # ============================================================

    async def _publish_signal(
        self,
        validated: Any,
    ) -> bool:

        try:

            result = (
                await self.signal_manager.publish(
                    validated
                )
            )

            return bool(result)

        except Exception:

            LOGGER.exception(
                "Failed to publish MEXC signal"
            )

            return False

    # ============================================================
    # LIVE EXECUTION
    # ============================================================

    async def _execute_signal(
        self,
        validated: Any,
    ) -> None:

        # ========================================================
        # HARD DISABLED
        # ========================================================
        #
        # Do NOT send an order from the scanner yet.
        #
        # Live execution requires:
        # - account balance risk sizing
        # - contract metadata
        # - leverage validation
        # - order reconciliation
        # - protective SL
        # - TP1 / TP2
        # - break-even
        # - emergency recovery
        #
        # ========================================================

        LOGGER.warning(
            "Live execution requested but remains disabled"
        )

        return
