from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .automation.mexc_client import MexcClient
from .config import Settings

LOGGER = logging.getLogger(__name__)

# User-facing aliases are intentionally mapped only to MEXC Futures-supported
# candle intervals.  The trading exchange is MEXC; there is no alternate
# exchange fallback in this market layer.
TIMEFRAME_ALIASES = {
    "1M": "1m", "1MIN": "1m",
    "5M": "5m", "5MIN": "5m",
    "15M": "15m", "15MIN": "15m",
    "30M": "30m", "30MIN": "30m",
    "1H": "1h", "1HR": "1h", "1HOUR": "1h",
    "4H": "4h", "4HR": "4h", "4HOUR": "4h",
    "8H": "8h", "8HR": "8h", "8HOUR": "8h",
    "1D": "1d", "1DAY": "1d",
    "1W": "1w", "1WEEK": "1w",
}

_MEXC_INTERVALS = {
    "1m": "Min1",
    "5m": "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h": "Min60",
    "4h": "Hour4",
    "8h": "Hour8",
    "1d": "Day1",
    "1w": "Week1",
}


@dataclass(frozen=True)
class MarketRef:
    exchange: str
    symbol: str


_SYMBOL_RE = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)?$")


def _normalize_mexc_symbol(value: str) -> str:
    clean = value.strip().upper().replace("/", "_").replace("-", "_")
    if clean.endswith("USDT") and "_" not in clean:
        clean = f"{clean[:-4]}_USDT"
    if not _SYMBOL_RE.fullmatch(clean):
        raise ValueError("Invalid MEXC Futures symbol.")
    return clean


class MarketData:
    """Single authoritative market-data adapter for MEXC Futures."""

    def __init__(self, settings: Settings, client: MexcClient | None = None) -> None:
        self.settings = settings
        self.mexc = client or MexcClient(settings)
        self._owns_client = client is None
        self._contracts: dict[str, dict] = {}
        self._last_discovery = 0.0
        self._discovery_lock = asyncio.Lock()

    async def start(self) -> None:
        # Keep startup lightweight.  The first command/scan performs discovery.
        return None

    async def close(self) -> None:
        if self._owns_client:
            await self.mexc.close()

    async def ensure_discovery(self, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not force and now - self._last_discovery < self.settings.discovery_refresh_seconds:
            return

        async with self._discovery_lock:
            now = asyncio.get_running_loop().time()
            if not force and now - self._last_discovery < self.settings.discovery_refresh_seconds:
                return

            rows = await self.mexc.get_contracts()
            contracts: dict[str, dict] = {}
            for item in rows:
                try:
                    symbol = str(item["symbol"]).upper()
                    if (
                        int(item.get("state", 99)) == 0
                        and bool(item.get("apiAllowed", False))
                        and not bool(item.get("isHidden", False))
                        and not bool(item.get("preMarket", False))
                        and str(item.get("quoteCoin", "")).upper() == "USDT"
                        and str(item.get("settleCoin", "")).upper() == "USDT"
                        and int(item.get("futureType", 1)) == 1
                    ):
                        contracts[symbol] = item
                except (TypeError, ValueError):
                    continue

            self._contracts = contracts
            self._last_discovery = now
            LOGGER.info("MEXC Futures market universe loaded: %d contracts", len(contracts))

    async def get_ticker(self, symbol: str) -> dict:
        return await self.mexc.get_ticker(_normalize_mexc_symbol(symbol))

    @staticmethod
    def _ticker_price(ticker: dict, *keys: str) -> Optional[float]:
        for key in keys:
            value = ticker.get(key)
            if value is None:
                continue
            try:
                price = float(value)
                if price > 0:
                    return price
            except (TypeError, ValueError):
                continue
        return None

    async def quote(self, symbol: str) -> dict[str, float | None]:
        ticker = await self.get_ticker(symbol)
        return {
            "last": self._ticker_price(ticker, "lastPrice", "last", "last_price", "price"),
            "bid": self._ticker_price(ticker, "bid1", "bidPrice", "bid", "bid_price"),
            "ask": self._ticker_price(ticker, "ask1", "askPrice", "ask", "ask_price"),
            "index": self._ticker_price(ticker, "indexPrice", "index", "index_price"),
            "fair": self._ticker_price(ticker, "fairPrice", "fair", "fair_price", "markPrice"),
        }

    async def price(self, symbol: str) -> Optional[float]:
        quote = await self.quote(symbol)
        return quote["last"] or quote["ask"] or quote["bid"]

    async def search(self, query: str, limit: int = 20) -> list[MarketRef]:
        await self.ensure_discovery()
        q = query.strip().upper().replace("/", "_").replace("-", "_")
        if q.endswith("USDT") and "_" not in q:
            q = f"{q[:-4]}_USDT"

        results: list[MarketRef] = []
        for symbol, item in self._contracts.items():
            base = str(item.get("baseCoin", "")).upper()
            if q in symbol or q == base or q.replace("_USDT", "") == base:
                results.append(MarketRef("mexc", symbol))
                if len(results) >= limit:
                    break
        return results

    async def resolve(self, raw: str) -> MarketRef:
        value = raw.strip()
        if ":" in value:
            exchange, value = value.split(":", 1)
            if exchange.strip().lower() != "mexc":
                raise ValueError("Only MEXC Futures markets are supported.")

        symbol = _normalize_mexc_symbol(value)
        await self.ensure_discovery()
        if symbol not in self._contracts:
            raise ValueError(f"MEXC Futures symbol was not found: {symbol}. Try SEARCH <coin>.")
        return MarketRef("mexc", symbol)

    async def ohlcv(self, ref: MarketRef, timeframe: str, limit: int):
        if ref.exchange.lower() != "mexc":
            raise ValueError("Only MEXC Futures market data is supported.")
        tf = TIMEFRAME_ALIASES.get(timeframe.upper(), timeframe.lower())
        interval = _MEXC_INTERVALS.get(tf)
        if interval is None:
            raise ValueError("MEXC Futures supports: 1M, 5M, 15M, 30M, 1H, 4H, 8H, 1D, 1W")
        return await self.mexc.get_klines(ref.symbol, interval, limit)

    async def executable_price(self, ref: MarketRef, side: str) -> float:
        quote = await self.quote(ref.symbol)
        side = side.upper()
        value = quote["ask"] if side == "LONG" else quote["bid"]
        if value is None:
            raise ValueError(f"MEXC executable {side} quote is unavailable for {ref.symbol}.")
        return float(value)
