from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
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
        self._semaphore = asyncio.Semaphore(max(1, settings.scan_concurrency))

    async def scan_once(self) -> dict[str, int]:
        symbols = await self.universe.refresh()
        if not symbols:
            LOGGER.warning("MEXC scanner universe is empty")
            return {"symbols": 0, "valid": 0, "sent": 0, "errors": 0}

        stats = {"symbols": len(symbols), "valid": 0, "sent": 0, "errors": 0}
        tasks = [asyncio.create_task(self._scan_one(symbol)) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                stats["errors"] += 1
                LOGGER.error("Scanner task failed: %s", result)
                continue
            stats["valid"] += int(result.get("valid", 0))
            stats["sent"] += int(result.get("sent", 0))
            stats["errors"] += int(result.get("errors", 0))

        LOGGER.info(
            "MEXC scan complete: symbols=%d valid=%d sent=%d errors=%d",
            stats["symbols"],
            stats["valid"],
            stats["sent"],
            stats["errors"],
        )
        return stats

    async def _scan_one(self, symbol: str) -> dict[str, int]:
        async with self._semaphore:
            try:
                raw_4h = await self.client.get_klines(symbol, MEXC_INTERVALS["4H"], self.settings.candle_limit)
                raw_1h = await self.client.get_klines(symbol, MEXC_INTERVALS["1H"], self.settings.candle_limit)
                raw_15m = await self.client.get_klines(symbol, MEXC_INTERVALS["15M"], self.settings.candle_limit)

                closed_4h = closed_candle_rows(raw_4h, "4h")
                closed_1h = closed_candle_rows(raw_1h, "1h")
                closed_15m = closed_candle_rows(raw_15m, "15m")

                analysis = analyze_candles(symbol, closed_4h, closed_1h, closed_15m)
                signal, reject_reasons = validate_signal(
                    analysis,
                    min_confluence=self.settings.min_confluence,
                    min_rr=self.settings.min_rr,
                    require_increasing_volume=self.settings.require_increasing_volume,
                )

                if signal is None:
                    return {"valid": 0, "sent": 0, "errors": 0}

                LOGGER.info(
                    "Valid MEXC setup: %s %s candle=%s score=%s rr=%.2f",
                    signal.symbol,
                    signal.side,
                    datetime.fromtimestamp(signal.candle_time / 1000, tz=timezone.utc).isoformat(),
                    analysis["score"],
                    signal.plan.rr,
                )

                sent = 0
                if self.settings.auto_signal_enabled:
                    created = await self.signal_manager.publish(signal)
                    sent = int(created)

                # Live execution remains behind explicit configuration and the
                # executor's own safety gate. The shipped config leaves it off.
                if self.settings.auto_trade_enabled:
                    meta = self.universe.get(symbol)
                    result = await self.executor.execute(signal, meta)
                    LOGGER.warning(
                        "Auto-trade gate result for %s: executed=%s message=%s",
                        symbol,
                        result.executed,
                        result.message,
                    )

                return {"valid": 1, "sent": sent, "errors": 0}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("MEXC symbol scan failed for %s: %s", symbol, exc)
                return {"valid": 0, "sent": 0, "errors": 1}
