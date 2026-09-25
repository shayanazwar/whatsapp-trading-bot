from __future__ import annotations

import asyncio
import time
from typing import Any

from ..analysis.engine import (
    analyze_candles,
    closed_candle_rows,
)
from .mexc_client import MexcClient
from .signal_manager import SignalManager
from .signal_validator import validate_signal
from .universe import UniverseSelector


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

        semaphore = asyncio.Semaphore(
            int(
                getattr(
                    self.settings,
                    "scanner_concurrency",
                    5,
                )
            )
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
                # ------------------------------------------------
                # 1. FETCH MULTI-TIMEFRAME MEXC FUTURES DATA
                # ------------------------------------------------

                candles_4h_raw, candles_1h_raw, candles_15m_raw, candles_5m_raw = (
                    await asyncio.gather(
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
                )

                # ------------------------------------------------
                # 2. ONLY CLOSED CANDLES
                # ------------------------------------------------

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

                # ------------------------------------------------
                # 3. DATA SUFFICIENCY
                # ------------------------------------------------

                if len(closed_4h) < 205:
                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": (
                            "Insufficient closed 4H candles"
                        ),
                    }

                if len(closed_1h) < 205:
                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": (
                            "Insufficient closed 1H candles"
                        ),
                    }

                if len(closed_15m) < 80:
                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": (
                            "Insufficient closed 15M candles"
                        ),
                    }

                if len(closed_5m) < 30:
                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": (
                            "Insufficient closed 5M candles"
                        ),
                    }

                # ------------------------------------------------
                # 4. ANALYZE 4H / 1H / 15M
                # ------------------------------------------------

                analysis = analyze_candles(
                    symbol,
                    closed_4h,
                    closed_1h,
                    closed_15m,
                )

                # ------------------------------------------------
                # 5. ATTACH REAL 5M DATA
                #
                # The engine currently receives 4H/1H/15M
                # directly. We attach the CLOSED 5M candles here
                # so the next validation layer can use them.
                # ------------------------------------------------

                analysis["mexc_5m_rows"] = closed_5m

                analysis["closed_5m_candle_time"] = (
                    closed_5m[-1][0]
                )

                analysis["closed_5m_candles"] = len(
                    closed_5m
                )

                # ------------------------------------------------
                # 6. DO NOT CONTINUE IF NO SETUP
                # ------------------------------------------------

                setup = analysis.get(
                    "setup"
                )

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

                # ------------------------------------------------
                # 7. FRESH MEXC EXECUTABLE QUOTE
                # ------------------------------------------------

                ticker = await self.client.get_ticker(
                    symbol
                )

                if not ticker:
                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": (
                            "Missing MEXC ticker"
                        ),
                    }

                # ------------------------------------------------
                # 8. EXTRACT BID / ASK
                # ------------------------------------------------

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

                if bid <= 0 or ask <= 0:
                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": (
                            "Invalid MEXC bid/ask"
                        ),
                    }

                # ------------------------------------------------
                # 9. EXECUTABLE PRICE
                # ------------------------------------------------

                executable_price = (
                    ask
                    if setup == "LONG"
                    else bid
                )

                # ------------------------------------------------
                # 10. SPREAD
                # ------------------------------------------------

                mid = (
                    (bid + ask) / 2.0
                )

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

                if spread_pct > max_spread_pct:
                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": (
                            f"Spread too high: "
                            f"{spread_pct:.6f}"
                        ),
                    }

                analysis["mexc_bid"] = bid
                analysis["mexc_ask"] = ask
                analysis["mexc_last"] = last
                analysis["mexc_spread_pct"] = (
                    spread_pct
                )

                # ------------------------------------------------
                # 11. INDEX / FAIR PRICE
                # ------------------------------------------------

                index_price = self._safe_float(
                    ticker.get("indexPrice")
                    or ticker.get("index")
                )

                fair_price = self._safe_float(
                    ticker.get("fairPrice")
                    or ticker.get("fair")
                    or ticker.get("markPrice")
                    or ticker.get("mark")
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

                if (
                    reference_price > 0
                    and executable_price > 0
                ):

                    dislocation = abs(
                        executable_price
                        - reference_price
                    ) / reference_price

                    analysis[
                        "mexc_reference_dislocation_pct"
                    ] = dislocation

                    max_dislocation = float(
                        getattr(
                            self.settings,
                            "max_price_dislocation_pct",
                            0.005,
                        )
                    )

                    if (
                        dislocation
                        > max_dislocation
                    ):
                        return {
                            "valid": False,
                            "sent": False,
                            "error": False,
                            "symbol": symbol,
                            "reason": (
                                "Executable price "
                                "too far from "
                                "MEXC reference"
                            ),
                        }

                # ------------------------------------------------
                # 12. MEXC MARKET CONTEXT
                # ------------------------------------------------

                funding = await self._safe_funding(
                    symbol
                )

                if funding is not None:
                    analysis[
                        "mexc_funding_rate"
                    ] = funding

                # ------------------------------------------------
                # 13. ORDERBOOK / DEPTH
                # ------------------------------------------------

                orderbook = await self._safe_orderbook(
                    symbol
                )

                if orderbook:
                    depth_data = self._calculate_depth(
                        orderbook
                    )

                    analysis[
                        "mexc_orderbook"
                    ] = orderbook

                    analysis.update(
                        depth_data
                    )

                # ------------------------------------------------
                # 14. RECENT DEALS / BUY-SELL FLOW
                # ------------------------------------------------

                deals = await self._safe_deals(
                    symbol
                )

                if deals:
                    flow = self._calculate_trade_flow(
                        deals
                    )

                    analysis[
                        "mexc_deals"
                    ] = deals

                    analysis.update(
                        flow
                    )

                # ------------------------------------------------
                # 15. UPDATE FUTURES CONTEXT
                # ------------------------------------------------

                analysis[
                    "futures_context"
                ] = "AVAILABLE"

                # ------------------------------------------------
                # 16. EXECUTABLE ENTRY DRIFT
                # ------------------------------------------------

                planned_entry = self._safe_float(
                    analysis.get("entry")
                )

                if planned_entry <= 0:
                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": (
                            "Invalid planned entry"
                        ),
                    }

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
                    return {
                        "valid": False,
                        "sent": False,
                        "error": False,
                        "symbol": symbol,
                        "reason": (
                            "Executable entry "
                            "drift exceeds limit"
                        ),
                        "analysis": analysis,
                    }

                # ------------------------------------------------
                # 17. REPRICE PLAN AROUND REAL EXECUTABLE QUOTE
                # ------------------------------------------------

                self._reprice_levels(
                    analysis,
                    executable_price,
                )

                # ------------------------------------------------
                # 18. FINAL DETERMINISTIC VALIDATION
                # ------------------------------------------------

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

                # ------------------------------------------------
                # 19. PUBLISH SIGNAL
                # ------------------------------------------------

                sent = False

                auto_signals = bool(
                    getattr(
                        self.settings,
                        "auto_signals_enabled",
                        False,
                    )
                )

                if auto_signals:
                    sent = await self._publish_signal(
                        validated
                    )

                # ------------------------------------------------
                # 20. LIVE EXECUTION REMAINS DISABLED
                # ------------------------------------------------

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
                return {
                    "valid": False,
                    "sent": False,
                    "error": True,
                    "symbol": symbol,
                    "reason": str(exc),
                }

    # ============================================================
    # SAFE HELPERS
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

    async def _safe_funding(
        self,
        symbol: str,
    ) -> float | None:

        try:

            method = getattr(
                self.client,
                "get_funding_rate",
                None,
            )

            if method is None:
                return None

            result = await method(
                symbol
            )

            if isinstance(
                result,
                dict,
            ):
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

            else:
                value = result

            if value is None:
                return None

            return float(value)

        except Exception:
            return None

    async def _safe_orderbook(
        self,
        symbol: str,
    ) -> dict[str, Any] | None:

        try:

            method = getattr(
                self.client,
                "get_depth",
                None,
            )

            if method is None:
                method = getattr(
                    self.client,
                    "get_orderbook",
                    None,
                )

            if method is None:
                return None

            result = await method(
                symbol
            )

            if isinstance(
                result,
                dict,
            ):
                return result

            return None

        except Exception:
            return None

    async def _safe_deals(
        self,
        symbol: str,
    ) -> list[Any] | None:

        try:

            method = getattr(
                self.client,
                "get_deals",
                None,
            )

            if method is None:
                method = getattr(
                    self.client,
                    "get_recent_trades",
                    None,
                )

            if method is None:
                return None

            result = await method(
                symbol
            )

            if isinstance(
                result,
                list,
            ):
                return result

            if isinstance(
                result,
                dict,
            ):
                deals = (
                    result.get("data")
                    or result.get("deals")
                    or result.get("trades")
                )

                if isinstance(
                    deals,
                    list,
                ):
                    return deals

            return None

        except Exception:
            return None

    # ============================================================
    # ORDERBOOK ANALYSIS
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
                            level.get("quantity")
                            or level.get("qty")
                            or level.get("volume")
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

        bid_depth = total_volume(
            bids
        )

        ask_depth = total_volume(
            asks
        )

        total_depth = (
            bid_depth + ask_depth
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
        deals: list[Any],
    ) -> dict[str, Any]:

        buy_volume = 0.0
        sell_volume = 0.0

        for deal in deals:

            try:

                if isinstance(
                    deal,
                    dict,
                ):

                    quantity = (
                        deal.get("quantity")
                        or deal.get("qty")
                        or deal.get("volume")
                        or 0
                    )

                    side = str(
                        deal.get("side")
                        or deal.get("type")
                        or ""
                    ).lower()

                else:

                    quantity = (
                        deal[1]
                        if len(deal) > 1
                        else 0
                    )

                    side = str(
                        deal[2]
                        if len(deal) > 2
                        else ""
                    ).lower()

                quantity = float(
                    quantity
                )

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

        analysis["entry"] = (
            executable_price
        )

        analysis["stop_loss"] = new_sl
        analysis["tp1"] = new_tp1
        analysis["tp2"] = new_tp2

        risk = abs(
            executable_price
            - new_sl
        )

        reward = abs(
            new_tp2
            - executable_price
        )

        analysis["rr"] = (
            reward / risk
            if risk > 0
            else 0.0
        )

    # ============================================================
    # SIGNAL PUBLISH
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

            if isinstance(
                result,
                bool,
            ):
                return result

            return True

        except Exception:
            return False

    # ============================================================
    # LIVE EXECUTION
    # ============================================================

    async def _execute_signal(
        self,
        validated: Any,
    ) -> None:

        # Intentionally left disabled at this stage.
        #
        # Live MEXC execution must remain behind:
        #
        # AUTO_TRADE_ENABLED
        # LIVE_EXECUTION_ALLOWED
        # LIVE_IMPLEMENTED
        #
        # until backtesting, paper trading,
        # reconciliation and protection are complete.

        return
