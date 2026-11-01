from __future__ import annotations

import pytest

from app.services.universe import top_usdt_perpetual_symbols


class DummyMexc:
    def __init__(self, contracts, tickers, per_symbol=None):
        self._contracts = contracts
        self._tickers = tickers
        self._per_symbol = per_symbol or {}
        self.ticker_calls = []

    async def contract_details(self):
        return [dict(row) for row in self._contracts]

    async def all_tickers(self):
        return [dict(row) for row in self._tickers]

    async def ticker(self, symbol):
        self.ticker_calls.append(symbol)
        if symbol not in self._per_symbol:
            raise RuntimeError("missing ticker")
        return dict(self._per_symbol[symbol])


def _contract(symbol, *, quote="USDT", settle="USDT", state=0, future_type=1):
    return {
        "symbol": symbol, "quoteCoin": quote, "settleCoin": settle,
        "state": state, "futureType": future_type, "contractSize": 1.0,
    }


def _ticker(symbol, turnover, price=100.0):
    return {"symbol": symbol, "amount24": turnover, "lastPrice": price}


@pytest.mark.asyncio
async def test_top_universe_filters_to_active_usdt_perpetuals_and_sorts_by_turnover():
    mexc = DummyMexc(
        [
            _contract("BTC_USDT"),
            _contract("ETH_USDT"),
            _contract("SOL_USDT"),
            _contract("BTC_USDC", quote="USDC", settle="USDC"),
            _contract("OLD_USDT", state=1),
            _contract("DELIVERY_USDT", future_type=2),
        ],
        [
            _ticker("BTC_USDT", 500), _ticker("ETH_USDT", 900),
            _ticker("SOL_USDT", 700), _ticker("OLD_USDT", 1000),
            _ticker("DELIVERY_USDT", 2000), _ticker("BTC_USDC", 3000),
        ],
    )
    rows = await top_usdt_perpetual_symbols(mexc, limit=2)
    assert [row["symbol"] for row in rows] == ["ETH_USDT", "SOL_USDT"]
    assert all(row["contract"]["futureType"] == 1 for row in rows)
    assert all(row["turnover_24h"] > 0 for row in rows)


@pytest.mark.asyncio
async def test_top_universe_does_not_invent_symbols_or_keep_zero_turnover():
    mexc = DummyMexc(
        [_contract("BTC_USDT"), _contract("ETH_USDT")],
        [_ticker("BTC_USDT", 0), _ticker("ETH_USDT", 100, price=0)],
    )
    assert await top_usdt_perpetual_symbols(mexc, limit=100) == []


@pytest.mark.asyncio
async def test_universe_ticker_fallback_is_used_if_bulk_endpoint_returns_one_row():
    contracts = [_contract("BTC_USDT"), _contract("ETH_USDT"), _contract("SOL_USDT")]
    mexc = DummyMexc(
        contracts,
        [_ticker("BTC_USDT", 10)],
        {
            "BTC_USDT": _ticker("BTC_USDT", 10),
            "ETH_USDT": _ticker("ETH_USDT", 30),
            "SOL_USDT": _ticker("SOL_USDT", 20),
        },
    )
    rows = await top_usdt_perpetual_symbols(mexc, limit=3)
    assert [row["symbol"] for row in rows] == ["ETH_USDT", "SOL_USDT", "BTC_USDT"]
    assert set(mexc.ticker_calls) == {"BTC_USDT", "ETH_USDT", "SOL_USDT"}
