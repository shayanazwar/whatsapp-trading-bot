from __future__ import annotations

import pytest

from app.market.candles import Candle
from app.mexc.client import MexcError
from app.services.market_data import MarketDataService


class MissingRealFieldsClient:
    async def get_klines_window(self, symbol, timeframe, **kwargs):
        start = kwargs["start"]
        step = {"1D": 86400, "4H": 14400, "1H": 3600}[timeframe]
        count = 3
        aligned = start - start % step
        return [Candle(symbol, timeframe, aligned + i * step, 100, 101, 99, 100.5,
                       volume=1, amount=100.5, complete=True) for i in range(count)]

    async def contract_detail(self, symbol):
        return {"symbol": symbol, "state": 0, "apiAllowed": True}


@pytest.mark.asyncio
async def test_real_price_series_never_silently_mixes_standard_ohlc():
    service = MarketDataService(MissingRealFieldsClient(), price_series="real")
    with pytest.raises(MexcError, match="real OHLC series is incomplete"):
        await service.fetch_symbol("BTCUSDT", window_days=1, daily_warmup_days=200,
                                   extra_intraday_warmup_days=1, now_epoch=1793000000)


@pytest.mark.asyncio
async def test_standard_price_series_does_not_require_real_fields():
    service = MarketDataService(MissingRealFieldsClient(), price_series="standard")
    data = await service.fetch_symbol("BTCUSDT", window_days=1, daily_warmup_days=200,
                                     extra_intraday_warmup_days=1, now_epoch=1793000000)
    assert data.timeframes["1H"]
    assert data.timeframes["1H"][0].open == 100
