from __future__ import annotations

import logging
from dataclasses import dataclass

from .mexc_client import MexcClient

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContractMeta:
    symbol: str
    quote_coin: str
    settle_coin: str
    contract_size: float
    price_unit: float
    vol_unit: float
    min_vol: float
    max_vol: float
    price_scale: int
    vol_scale: int
    state: int
    api_allowed: bool
    hidden: bool
    future_type: int
    pre_market: bool


class MexcUniverse:
    def __init__(self, client: MexcClient, max_symbols: int, test_symbols: list[str] | None = None) -> None:
        self.client = client
        self.max_symbols = max(1, min(max_symbols, 500))
        self.test_symbols = {value.upper() for value in (test_symbols or [])}
        self._contracts: dict[str, ContractMeta] = {}
        self._last_symbols: list[str] = []

    async def refresh(self) -> list[str]:
        raw = await self.client.get_contracts()
        contracts: dict[str, ContractMeta] = {}

        for item in raw:
            try:
                symbol = str(item["symbol"]).upper()
                meta = ContractMeta(
                    symbol=symbol,
                    quote_coin=str(item.get("quoteCoin", "")).upper(),
                    settle_coin=str(item.get("settleCoin", "")).upper(),
                    contract_size=float(item.get("contractSize", 0)),
                    price_unit=float(item.get("priceUnit", 0)),
                    vol_unit=float(item.get("volUnit", 0)),
                    min_vol=float(item.get("minVol", 0)),
                    max_vol=float(item.get("maxVol", 0)),
                    price_scale=int(item.get("priceScale", 8)),
                    vol_scale=int(item.get("volScale", 8)),
                    state=int(item.get("state", 99)),
                    api_allowed=bool(item.get("apiAllowed", False)),
                    hidden=bool(item.get("isHidden", False)),
                    future_type=int(item.get("futureType", 1)),
                    pre_market=bool(item.get("preMarket", False)),
                )
            except (KeyError, TypeError, ValueError):
                continue

            # Scanner scope: live, API-allowed, USDT-settled perpetual contracts.
            if meta.state != 0:
                continue
            if int(item.get("type", 1) or 1) != 1:
                continue
            if not meta.api_allowed:
                continue
            if meta.quote_coin != "USDT" or meta.settle_coin != "USDT":
                continue
            if meta.future_type != 1 or meta.pre_market:
                continue
            if meta.hidden:
                continue
            if meta.contract_size <= 0 or meta.vol_unit <= 0:
                continue

            contracts[symbol] = meta

        self._contracts = contracts

        if self.test_symbols:
            symbols = [symbol for symbol in self.test_symbols if symbol in contracts]
            missing = sorted(self.test_symbols.difference(symbols))
            if missing:
                LOGGER.warning("TEST_SYMBOLS not available on MEXC: %s", ",".join(missing))
        else:
            try:
                tickers = await self.client.get_tickers()
                turnover = {
                    str(item.get("symbol", "")).upper(): float(item.get("amount24", 0) or 0)
                    for item in tickers
                }
            except Exception as exc:
                LOGGER.warning("MEXC ticker universe ranking unavailable: %s", exc)
                turnover = {}

            symbols = sorted(
                contracts,
                key=lambda symbol: (turnover.get(symbol, 0.0), symbol),
                reverse=True,
            )[: self.max_symbols]

        self._last_symbols = symbols
        LOGGER.info("MEXC scanner universe refreshed: %d symbols", len(symbols))
        return list(symbols)

    def symbols(self) -> list[str]:
        return list(self._last_symbols)

    def get(self, symbol: str) -> ContractMeta:
        key = symbol.upper()
        try:
            return self._contracts[key]
        except KeyError as exc:
            raise ValueError(f"MEXC contract metadata not loaded for {key}") from exc
