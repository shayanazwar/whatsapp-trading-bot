from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ..analysis.engine import analyze_candles, closed_candle_rows
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
}


class MexcScanner:
    def __init__(
        self,
        *,
        client: MexcClient,
        settings: Settings,
        universe: MexcUniverse,
        signal_manager: SignalManager,
        executor: MexcExecutor,
    ) -> None:
        self.client = client
        self.settings = settings
        self.universe = universe
        self.signal_manager = signal_manager
        self.executor = executor
        self._semaphore = asyncio.Semaphore(
            max(1, settings.scan_concurrency)
        )

    async def scan_once(self) -> dict[str, int]:
        symbols = await self.universe.refresh()

        if not symbols:
            LOGGER.warning(
                "MEXC scanner universe is empty"
            )
            return {
                "symbols": 0,
                "valid": 0,
                "sent": 0,
                "errors": 0,
            }

        stats = {
            "symbols": len(symbols),
            "valid": 0,
            "sent": 0,
            "errors": 0,
        }

        tasks = [
            asyncio.create_task(
                self._scan_one(symbol)
            )
            for symbol in symbols
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                stats["errors"] += 1
                LOGGER.error(
                    "Scanner task failed: %s",
                    result,
                )
                continue

            stats["valid"] += int(
                result.get("valid", 0)
            )
            stats["sent"] += int(
                result.get("sent", 0)
            )
            stats["errors"] += int(
                result.get("errors", 0)
            )

        LOGGER.info(
            "MEXC scan complete: symbols=%d valid=%d sent=%d errors=%d",
            stats["symbols"],
            stats["valid"],
            stats["sent"],
            stats["errors"],
        )

        return stats

    def _log_rejection(
        self,
        symbol: str,
        reason: str,
        *,
        analysis: dict[str, Any] | None = None,
    ) -> None:
        """
        Centralized rejection logging.

        This does not change any trading rule.
        It only makes the reason visible in Render logs.
        """
        if analysis is None:
            LOGGER.info(
                "MEXC REJECT | %s | %s",
                symbol,
                reason,
            )
            return

        setup = analysis.get("setup")
        score = analysis.get("score")
        rr = analysis.get("rr")

        LOGGER.info(
            "MEXC REJECT | %s | reason=%s | setup=%s | score=%s | rr=%s",
            symbol,
            reason,
            setup,
            score,
            rr,
        )

    async def _scan_one(
        self,
        symbol: str,
    ) -> dict[str, int]:

        async with self._semaphore:

            try:
                # ============================================================
                # 1. FETCH MULTI-TIMEFRAME MEXC FUTURES DATA
                # ============================================================

                raw_4h = await self.client.get_klines(
                    symbol,
                    MEXC_INTERVALS["4H"],
                    self.settings.candle_limit,
                )

                raw_1h = await self.client.get_klines(
                    symbol,
                    MEXC_INTERVALS["1H"],
                    self.settings.candle_limit,
                )

                raw_15m = await self.client.get_klines(
                    symbol,
                    MEXC_INTERVALS["15M"],
                    self.settings.candle_limit,
                )

                raw_5m = await self.client.get_klines(
                    symbol,
                    MEXC_INTERVALS["5M"],
                    self.settings.candle_limit,
                )

                # ============================================================
                # 2. ONLY USE CLOSED CANDLES
                # ============================================================

                closed_4h = closed_candle_rows(
                    raw_4h,
                    "4h",
                )

                closed_1h = closed_candle_rows(
                    raw_1h,
                    "1h",
                )

                closed_15m = closed_candle_rows(
                    raw_15m,
                    "15m",
                )

                closed_5m = closed_candle_rows(
                    raw_5m,
                    "5m",
                )

                # ============================================================
                # 3. ANALYSIS ENGINE
                # ============================================================

                analysis = analyze_candles(
                    symbol,
                    closed_4h,
                    closed_1h,
                    closed_15m,
                )

                analysis["mexc_5m_rows"] = closed_5m

                # ============================================================
                # 4. LOG INITIAL ANALYSIS RESULT
                # ============================================================

                setup = analysis.get("setup")
                score = analysis.get("score")
                rr = analysis.get("rr")

                LOGGER.info(
                    "MEXC ANALYSIS | %s | setup=%s | score=%s | rr=%s",
                    symbol,
                    setup,
                    score,
                    rr,
                )

                # If the analysis engine did not produce LONG/SHORT,
                # there is no reason to request expensive execution context.
                if setup not in {"LONG", "SHORT"}:
                    reason = (
                        analysis.get("reason")
                        or analysis.get("rejection_reason")
                        or "Analysis engine produced no valid LONG/SHORT setup"
                    )

                    self._log_rejection(
                        symbol,
                        str(reason),
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                # ============================================================
                # 5. FRESH MEXC EXECUTABLE QUOTE
                # ============================================================

                ticker = await self.client.get_ticker(
                    symbol
                )

                now_ms = int(
                    datetime.now(
                        tz=timezone.utc
                    ).timestamp()
                    * 1000
                )

                ticker_ts = int(
                    ticker.get(
                        "timestamp",
                        0,
                    )
                    or 0
                )

                if (
                    ticker_ts <= 0
                    or now_ms - ticker_ts
                    > int(
                        self.settings.max_data_age_seconds
                        * 1000
                    )
                ):
                    self._log_rejection(
                        symbol,
                        "Stale MEXC ticker",
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                last_price = float(
                    ticker.get(
                        "lastPrice",
                        0,
                    )
                    or 0
                )

                bid = float(
                    ticker.get(
                        "bid1",
                        0,
                    )
                    or 0
                )

                ask = float(
                    ticker.get(
                        "ask1",
                        0,
                    )
                    or 0
                )

                if (
                    min(
                        last_price,
                        bid,
                        ask,
                    )
                    <= 0
                    or ask < bid
                ):
                    self._log_rejection(
                        symbol,
                        "Invalid MEXC executable quote",
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                # ============================================================
                # 6. SPREAD FILTER
                # ============================================================

                spread_pct = (
                    (ask - bid)
                    / ((ask + bid) / 2.0)
                )

                if (
                    spread_pct
                    > self.settings.max_mexc_spread_pct
                ):
                    self._log_rejection(
                        symbol,
                        f"Wide MEXC spread: {spread_pct:.5f}",
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                # ============================================================
                # 7. INDEX / FAIR PRICE VALIDATION
                # ============================================================

                index_price = float(
                    ticker.get(
                        "indexPrice",
                        0,
                    )
                    or 0
                )

                fair_price = float(
                    ticker.get(
                        "fairPrice",
                        0,
                    )
                    or 0
                )

                if (
                    index_price <= 0
                    or fair_price <= 0
                ):
                    self._log_rejection(
                        symbol,
                        "Missing MEXC index/fair price",
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                index_dislocation = (
                    abs(
                        last_price
                        - index_price
                    )
                    / index_price
                )

                fair_dislocation = (
                    abs(
                        last_price
                        - fair_price
                    )
                    / fair_price
                )

                if (
                    max(
                        index_dislocation,
                        fair_dislocation,
                    )
                    > self.settings.max_index_dislocation_pct
                ):
                    self._log_rejection(
                        symbol,
                        (
                            "MEXC price dislocation: "
                            f"index={index_dislocation:.5f} "
                            f"fair={fair_dislocation:.5f}"
                        ),
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                # ============================================================
                # 8. ORDERBOOK
                # ============================================================

                depth = await self.client.get_depth(
                    symbol,
                    self.settings.orderbook_levels,
                )

                bids = depth.get("bids") or []
                asks = depth.get("asks") or []

                if not bids or not asks:
                    self._log_rejection(
                        symbol,
                        "Missing MEXC orderbook",
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                # ============================================================
                # 9. FUNDING + TRADE FLOW
                # ============================================================

                funding = await self.client.get_funding_rate(
                    symbol
                )

                deals = await self.client.get_deals(
                    symbol,
                    self.settings.trade_flow_limit,
                )

                buy_volume = sum(
                    float(
                        x.get("v", 0)
                        or 0
                    )
                    for x in deals
                    if int(
                        x.get("T", 0)
                        or 0
                    )
                    == 1
                )

                sell_volume = sum(
                    float(
                        x.get("v", 0)
                        or 0
                    )
                    for x in deals
                    if int(
                        x.get("T", 0)
                        or 0
                    )
                    == 2
                )

                total_flow = (
                    buy_volume
                    + sell_volume
                )

                delta_proxy = (
                    (
                        buy_volume
                        - sell_volume
                    )
                    / total_flow
                    if total_flow > 0
                    else 0.0
                )

                ticker["spread_pct"] = spread_pct
                ticker[
                    "index_dislocation_pct"
                ] = index_dislocation
                ticker[
                    "fair_dislocation_pct"
                ] = fair_dislocation
                ticker["orderbook"] = depth
                ticker["funding"] = funding
                ticker["buy_volume"] = buy_volume
                ticker["sell_volume"] = sell_volume
                ticker["delta_proxy"] = delta_proxy

                analysis["mexc_context"] = ticker

                # ============================================================
                # 10. SELECT EXECUTABLE SIDE
                # ============================================================

                quote_keys = (
                    (
                        "ask",
                        "ask1",
                        "askPrice",
                    )
                    if analysis["setup"] == "LONG"
                    else (
                        "bid",
                        "bid1",
                        "bidPrice",
                    )
                )

                executable_price = None

                for key in quote_keys:
                    value = ticker.get(key)

                    if value is None:
                        continue

                    try:
                        candidate = float(value)

                        if candidate > 0:
                            executable_price = candidate
                            break

                    except (
                        TypeError,
                        ValueError,
                    ):
                        pass

                if executable_price is None:
                    self._log_rejection(
                        symbol,
                        "No valid executable MEXC price",
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                # ============================================================
                # 11. ENTRY DRIFT PROTECTION
                # ============================================================

                planned_entry = float(
                    analysis["entry"]
                )

                drift = (
                    abs(
                        executable_price
                        - planned_entry
                    )
                    / planned_entry
                )

                if (
                    drift
                    > max(
                        0.0,
                        self.settings.max_entry_drift_pct,
                    )
                ):
                    self._log_rejection(
                        symbol,
                        (
                            "Executable entry drift too large: "
                            f"planned={planned_entry} "
                            f"executable={executable_price} "
                            f"drift={drift:.4f}"
                        ),
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                # ============================================================
                # 12. REPRICE CURRENT PROTOTYPE PLAN
                # ============================================================

                delta = (
                    executable_price
                    - planned_entry
                )

                analysis["entry"] = (
                    executable_price
                )

                analysis["stop_loss"] = (
                    float(
                        analysis["stop_loss"]
                    )
                    + delta
                )

                analysis["tp1"] = (
                    float(
                        analysis["tp1"]
                    )
                    + delta
                )

                analysis["tp2"] = (
                    float(
                        analysis["tp2"]
                    )
                    + delta
                )

                stop_distance = abs(
                    analysis["entry"]
                    - analysis["stop_loss"]
                )

                target_distance = abs(
                    analysis["tp2"]
                    - analysis["entry"]
                )

                if stop_distance <= 0:
                    self._log_rejection(
                        symbol,
                        "Invalid stop-loss distance after repricing",
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                analysis["rr"] = (
                    target_distance
                    / stop_distance
                )

                analysis[
                    "mexc_executable_entry"
                ] = executable_price

                analysis[
                    "mexc_entry_drift_pct"
                ] = drift

                # ============================================================
                # 13. FINAL VALIDATOR
                # ============================================================

                signal, reject_reasons = validate_signal(
                    analysis,
                    min_confluence=(
                        self.settings.min_confluence
                    ),
                    min_rr=(
                        self.settings.min_rr
                    ),
                    require_increasing_volume=(
                        self.settings.require_increasing_volume
                    ),
                )

                # ============================================================
                # 14. IMPORTANT:
                # LOG THE REASONS THAT WERE PREVIOUSLY HIDDEN
                # ============================================================

                if signal is None:

                    if reject_reasons:
                        if isinstance(
                            reject_reasons,
                            (list, tuple, set),
                        ):
                            reason_text = "; ".join(
                                str(reason)
                                for reason in reject_reasons
                            )
                        else:
                            reason_text = str(
                                reject_reasons
                            )
                    else:
                        reason_text = (
                            "Validator rejected setup "
                            "without returning a reason"
                        )

                    self._log_rejection(
                        symbol,
                        reason_text,
                        analysis=analysis,
                    )

                    return {
                        "valid": 0,
                        "sent": 0,
                        "errors": 0,
                    }

                # ============================================================
                # 15. VALID SETUP
                # ============================================================

                LOGGER.info(
                    "MEXC VALID SETUP | %s | side=%s | candle=%s | score=%s | rr=%.2f",
                    signal.symbol,
                    signal.side,
                    datetime.fromtimestamp(
                        signal.candle_time / 1000,
                        tz=timezone.utc,
                    ).isoformat(),
                    analysis["score"],
                    signal.plan.rr,
                )

                sent = 0

                # ============================================================
                # 16. WHATSAPP SIGNAL
                # ============================================================

                if self.settings.auto_signal_enabled:

                    created = (
                        await self.signal_manager.publish(
                            signal
                        )
                    )

                    sent = int(created)

                    LOGGER.info(
                        "MEXC SIGNAL PUBLISHED | %s | side=%s | sent=%s",
                        signal.symbol,
                        signal.side,
                        sent,
                    )

                # ============================================================
                # 17. AUTO TRADE
                # ============================================================

                if self.settings.auto_trade_enabled:

                    meta = self.universe.get(
                        symbol
                    )

                    result = (
                        await self.executor.execute(
                            signal,
                            meta,
                        )
                    )

                    LOGGER.warning(
                        "Auto-trade gate result for %s: executed=%s message=%s",
                        symbol,
                        result.executed,
                        result.message,
                    )

                return {
                    "valid": 1,
                    "sent": sent,
                    "errors": 0,
                }

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                LOGGER.exception(
                    "MEXC symbol scan failed for %s: %s",
                    symbol,
                    exc,
                )

                return {
                    "valid": 0,
                    "sent": 0,
                    "errors": 1,
                }
