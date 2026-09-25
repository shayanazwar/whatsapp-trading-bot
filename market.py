from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
import websockets

from .config import Settings

LOGGER = logging.getLogger(__name__)


# Binance chart timeframe aliases
TIMEFRAME_ALIASES = {
    # Minutes
    "1M": "1m",
    "1MIN": "1m",
    "3M": "3m",
    "3MIN": "3m",
    "5M": "5m",
    "5MIN": "5m",
    "15M": "15m",
    "15MIN": "15m",
    "30M": "30m",
    "30MIN": "30m",

    # Hours
    "1H": "1h",
    "1HR": "1h",
    "1HOUR": "1h",
    "2H": "2h",
    "2HR": "2h",
    "2HOUR": "2h",
    "4H": "4h",
    "4HR": "4h",
    "4HOUR": "4h",
    "6H": "6h",
    "6HR": "6h",
    "6HOUR": "6h",
    "8H": "8h",
    "8HR": "8h",
    "8HOUR": "8h",
    "12H": "12h",
    "12HR": "12h",
    "12HOUR": "12h",

    # Days
    "1D": "1d",
    "1DAY": "1d",
    "3D": "3d",
    "3DAY": "3d",

    # Week
    "1W": "1w",
    "1WEEK": "1w",
}


@dataclass(frozen=True)
class MarketRef:
    exchange: str
    symbol: str


class BinancePriceStream:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.prices: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="binance-price-stream")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def get(self, symbol: str) -> Optional[float]:
        return self.prices.get(symbol.upper().replace("/", ""))

    async def _run(self) -> None:
        delay = 1

        while not self._stopping.is_set():
            try:
                async with websockets.connect(
                    self.settings.binance_ws_url,
                    ping_interval=20,
                    ping_timeout=30,
                    close_timeout=5,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    LOGGER.info("Connected to Binance market-data stream")
                    delay = 1

                    async for raw in ws:
                        if self._stopping.is_set():
                            break

                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")

                        try:
                            payload = json.loads(raw)
                        except ValueError:
                            continue

                        if not isinstance(payload, list):
                            continue

                        for item in payload:
                            try:
                                self.prices[str(item["s"]).upper()] = float(item["c"])
                            except (KeyError, TypeError, ValueError):
                                continue

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                LOGGER.warning(
                    "Binance WebSocket disconnected: %s",
                    exc,
                )
                await asyncio.sleep(min(delay, 30))
                delay = min(delay * 2, 30)


class MarketData:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.binance_stream = BinancePriceStream(settings)
        self.http = httpx.AsyncClient(timeout=20)

        self.binance_symbols: dict[str, dict] = {}
        self.other_symbols: dict[str, list[str]] = {}

        self.last_discovery = 0.0
        self.discovery_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.binance_stream.start()

    async def close(self) -> None:
        await self.binance_stream.stop()
        await self.http.aclose()

    async def binance_price(
        self,
        symbol: str,
    ) -> Optional[float]:

        clean = symbol.upper().replace("/", "")

        price = self.binance_stream.get(clean)

        if price is not None:
            return price

        try:
            response = await self.http.get(
                f"{self.settings.binance_rest_url}/api/v3/ticker/price",
                params={"symbol": clean},
            )

            response.raise_for_status()

            return float(response.json()["price"])

        except Exception as exc:
            LOGGER.warning(
                "Binance price lookup failed for %s: %s",
                symbol,
                exc,
            )

            return None

    async def fetch_binance_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ):
        clean = symbol.upper().replace("/", "")

        response = await self.http.get(
            f"{self.settings.binance_rest_url}/api/v3/klines",
            params={
                "symbol": clean,
                "interval": timeframe,
                "limit": limit,
            },
        )

        response.raise_for_status()

        return response.json()

    async def ensure_discovery(
        self,
        force: bool = False,
    ) -> None:

        now = asyncio.get_running_loop().time()

        if (
            not force
            and now - self.last_discovery
            < self.settings.discovery_refresh_seconds
        ):
            return

        async with self.discovery_lock:

            now = asyncio.get_running_loop().time()

            if (
                not force
                and now - self.last_discovery
                < self.settings.discovery_refresh_seconds
            ):
                return

            self.binance_symbols = await self._load_binance_symbols()

            for exchange in self.settings.discovery_exchange_list:

                if exchange == "binance":
                    continue

                loader = {
                    "bybit": self._load_bybit_symbols,
                    "okx": self._load_okx_symbols,
                    "gateio": self._load_gate_symbols,
                    "kucoin": self._load_kucoin_symbols,
                }.get(exchange)

                if not loader:
                    continue

                try:
                    self.other_symbols[exchange] = await loader()

                except Exception as exc:
                    LOGGER.warning(
                        "Could not load %s symbols: %s",
                        exchange,
                        exc,
                    )

                    self.other_symbols[exchange] = []

            self.last_discovery = now

    async def _load_binance_symbols(
        self,
    ) -> dict[str, dict]:

        response = await self.http.get(
            f"{self.settings.binance_rest_url}/api/v3/exchangeInfo"
        )

        response.raise_for_status()

        data = response.json()

        result: dict[str, dict] = {}

        for item in data.get("symbols", []):

            if (
                item.get("status") == "TRADING"
                and item.get("isSpotTradingAllowed")
            ):
                result[item["symbol"]] = item

        return result

    async def _load_bybit_symbols(
        self,
    ) -> list[str]:

        response = await self.http.get(
            "https://api.bybit.com/v5/market/instruments-info",
            params={
                "category": "spot",
                "limit": 1000,
            },
        )

        response.raise_for_status()

        rows = response.json().get(
            "result",
            {},
        ).get(
            "list",
            [],
        )

        return [
            str(row.get("symbol"))
            for row in rows
            if row.get("status") == "Trading"
        ]

    async def _load_okx_symbols(
        self,
    ) -> list[str]:

        response = await self.http.get(
            "https://www.okx.com/api/v5/public/instruments",
            params={
                "instType": "SPOT",
            },
        )

        response.raise_for_status()

        rows = response.json().get(
            "data",
            [],
        )

        return [
            str(row.get("instId"))
            for row in rows
            if row.get("state") == "live"
        ]

    async def _load_gate_symbols(
        self,
    ) -> list[str]:

        response = await self.http.get(
            "https://api.gateio.ws/api/v4/spot/currency_pairs"
        )

        response.raise_for_status()

        rows = response.json()

        return [
            str(row.get("id"))
            for row in rows
            if row.get(
                "trade_status",
                "tradable",
            ) == "tradable"
        ]

    async def _load_kucoin_symbols(
        self,
    ) -> list[str]:

        response = await self.http.get(
            "https://api.kucoin.com/api/v2/symbols"
        )

        response.raise_for_status()

        rows = response.json().get(
            "data",
            [],
        )

        return [
            str(row.get("symbol"))
            for row in rows
            if row.get("enableTrading")
        ]

    async def search(
        self,
        query: str,
        limit: int = 20,
    ) -> list[MarketRef]:

        await self.ensure_discovery()

        q = (
            query
            .strip()
            .upper()
            .replace("/", "")
            .replace("-", "_")
        )

        results: list[MarketRef] = []
        seen: set[tuple[str, str]] = set()

        for symbol, item in self.binance_symbols.items():

            base = str(
                item.get(
                    "baseAsset",
                    "",
                )
            ).upper()

            quote = str(
                item.get(
                    "quoteAsset",
                    "",
                )
            ).upper()

            if quote not in {
                "USDT",
                "USDC",
                "FDUSD",
                "USD",
            }:
                continue

            if q not in symbol.upper() and q not in base:
                continue

            key = (
                "binance",
                symbol,
            )

            if key not in seen:

                results.append(
                    MarketRef(*key)
                )

                seen.add(key)

            if len(results) >= limit:
                return results

        for exchange, symbols in self.other_symbols.items():

            for symbol in symbols:

                normalized = (
                    symbol
                    .upper()
                    .replace("/", "")
                    .replace("-", "_")
                )

                if q not in normalized:
                    continue

                key = (
                    exchange,
                    symbol,
                )

                if key not in seen:

                    results.append(
                        MarketRef(*key)
                    )

                    seen.add(key)

                if len(results) >= limit:
                    return results

        return results

    async def resolve(
        self,
        raw: str,
    ) -> MarketRef:

        value = raw.strip()

        if ":" in value:

            exchange, symbol = value.split(
                ":",
                1,
            )

            exchange = exchange.strip().lower()
            symbol = symbol.strip().upper()

            await self.ensure_discovery()

            if exchange == "binance":

                clean = symbol.replace(
                    "/",
                    "",
                )

                if clean in self.binance_symbols:
                    return MarketRef(
                        "binance",
                        clean,
                    )

                raise ValueError(
                    "Binance symbol was not found. "
                    "Try SEARCH <coin>."
                )

            symbols = self.other_symbols.get(
                exchange,
                [],
            )

            if symbol in symbols:
                return MarketRef(
                    exchange,
                    symbol,
                )

            normalized = (
                symbol
                .replace("/", "")
                .replace("-", "_")
            )

            for candidate in symbols:

                if (
                    candidate
                    .upper()
                    .replace("/", "")
                    .replace("-", "_")
                    == normalized
                ):
                    return MarketRef(
                        exchange,
                        candidate,
                    )

            raise ValueError(
                f"Symbol was not found on {exchange}."
            )

        clean = value.upper().replace(
            "/",
            "",
        )

        await self.ensure_discovery()

        if clean in self.binance_symbols:
            return MarketRef(
                "binance",
                clean,
            )

        if (
            clean.isalnum()
            and clean.endswith(
                (
                    "USDT",
                    "USDC",
                    "USD",
                )
            )
        ):
            # If discovery failed temporarily,
            # permit a Binance-formatted symbol
            # and let REST validate it.
            return MarketRef(
                "binance",
                clean,
            )

        raise ValueError(
            "Symbol not found. Try SEARCH <coin>."
        )

    async def ohlcv(
        self,
        ref: MarketRef,
        timeframe: str,
        limit: int,
    ):

        tf = TIMEFRAME_ALIASES.get(
            timeframe.upper(),
            timeframe.lower(),
        )

        supported_timeframes = {
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
            "6h",
            "8h",
            "12h",
            "1d",
            "3d",
            "1w",
        }

        if tf not in supported_timeframes:

            raise ValueError(
                "Supported chart timeframes: "
                "1M, 3M, 5M, 15M, 30M, "
                "1H, 2H, 4H, 6H, 8H, 12H, "
                "1D, 3D, 1W"
            )

        # Binance is the reliable chart source in v1.
        # Search can discover symbols on other venues;
        # chart support for those venues is intentionally
        # disabled until their exact public candle schemas
        # are tested.

        if ref.exchange != "binance":

            raise ValueError(
                "Charts are currently generated from "
                "Binance spot data. Use SEARCH to find "
                "the Binance pair."
            )

        return await self.fetch_binance_ohlcv(
            ref.symbol,
            tf,
            limit,
        )
