from __future__ import annotations

import httpx
import pytest

from app.mexc import MexcClient, _candles_from_payload


def kline_payload(times):
    return {"success": True, "code": 0, "data": {
        "time": times, "open": [100.0] * len(times), "high": [101.0] * len(times),
        "low": [99.0] * len(times), "close": [100.5] * len(times), "vol": [1.0] * len(times),
    }}


def test_parse_array_kline_response():
    candles = _candles_from_payload(kline_payload([60, 120]))
    assert len(candles) == 2
    assert candles[0].time == 60 and candles[1].close == 100.5


def test_reject_failed_api_code():
    with pytest.raises(Exception):
        _candles_from_payload({"success": False, "code": 500, "data": None})


@pytest.mark.asyncio
async def test_candle_fetch_paginates_2000_and_validates_continuity(settings):
    calls = []
    async def handler(request):
        start, end = int(request.url.params["start"]), int(request.url.params["end"])
        calls.append((start, end))
        times = list(range(start, end + 1, 60))
        return httpx.Response(200, json=kline_payload(times))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mx = MexcClient(settings, client)
    batch = await mx.get_candle_batch(0, 2000 * 60)
    assert len(calls) == 2
    assert len(batch.candles) == 2001
    assert batch.complete
    await client.aclose()


@pytest.mark.asyncio
async def test_empty_first_page_does_not_skip_later_page(settings):
    calls = []
    async def handler(request):
        start, end = int(request.url.params["start"]), int(request.url.params["end"])
        calls.append(start)
        if len(calls) == 1:
            return httpx.Response(200, json=kline_payload([]))
        times = list(range(start, end + 1, 60))
        return httpx.Response(200, json=kline_payload(times))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mx = MexcClient(settings, client)
    batch = await mx.get_candle_batch(0, 2000 * 60)
    assert len(calls) == 2
    assert batch.missing_timestamps
    assert batch.candles[0].time == 2000 * 60
    await client.aclose()


@pytest.mark.asyncio
async def test_contract_parser_uses_country_metadata(settings):
    async def handler(request):
        return httpx.Response(200, json={"success": True, "code": 0, "data": {
            "symbol": "BTC_USDT", "contractSize": 0.0001, "priceUnit": 0.1,
            "volUnit": 1, "minVol": 1, "maxVol": 400000, "maxLeverage": 500,
            "countryConfigContractMaxLeverage": 0, "takerFeeRate": 0.0008,
            "maintenanceMarginRate": 0.001, "liquidationFeeRate": 0.0004,
        }})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mx = MexcClient(settings, client)
    contract = await mx.get_contract_info()
    assert contract.symbol == "BTC_USDT"
    assert contract.contract_size == 0.0001
    await client.aclose()


@pytest.mark.asyncio
async def test_out_of_order_candle_response_is_flagged(settings):
    async def handler(request):
        return httpx.Response(200, json=kline_payload([60, 0]))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mx = MexcClient(settings, client)
    batch = await mx.get_candle_batch(0, 60)
    assert batch.out_of_order_timestamps == (0,)
    assert not batch.complete
    await client.aclose()


@pytest.mark.asyncio
async def test_ticker_uses_last_price_and_converts_milliseconds(settings):
    async def handler(request):
        return httpx.Response(200, json={"success": True, "code": 0, "data": {
            "symbol": "BTC_USDT", "lastPrice": 80000.5, "timestamp": 1_800_000_000_000,
        }})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mx = MexcClient(settings, client)
    price, timestamp = await mx.get_ticker()
    assert price == 80000.5
    assert timestamp == 1_800_000_000
    await client.aclose()


@pytest.mark.asyncio
async def test_nontransient_http_error_is_not_retried(settings):
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"success": False, "code": 400})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mx = MexcClient(settings, client)
    with pytest.raises(Exception):
        await mx.get_contract_info()
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_duplicate_candle_timestamp_is_flagged(settings):
    async def handler(request):
        return httpx.Response(200, json=kline_payload([0, 60, 60]))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mx = MexcClient(settings, client)
    batch = await mx.get_candle_batch(0, 60)
    assert batch.duplicate_timestamps == (60,)
    assert not batch.complete
    await client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_response_retries(settings):
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"})
        return httpx.Response(200, json={"success": True, "code": 0, "data": {
            "symbol": "BTC_USDT", "lastPrice": 80000, "timestamp": 1_800_000_000_000,
        }})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    retry_settings = settings.__class__(**{**settings.__dict__, "mexc_max_retries": 1})
    mx = MexcClient(retry_settings, client)
    price, _ = await mx.get_ticker()
    assert price == 80000
    assert calls == 2
    await client.aclose()

@pytest.mark.asyncio
async def test_contract_parser_rejects_missing_taker_fee_instead_of_assuming_zero(settings):
    async def handler(request):
        return httpx.Response(200, json={"success": True, "code": 0, "data": {
            "symbol": "BTC_USDT", "contractSize": 0.0001, "priceUnit": 0.1,
            "volUnit": 1, "minVol": 1, "maxVol": 400000, "maxLeverage": 500,
            "countryConfigContractMaxLeverage": 0,
            "maintenanceMarginRate": 0.001, "liquidationFeeRate": 0.0004,
        }})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mx = MexcClient(settings, client)
    with pytest.raises(Exception, match="metadata is incomplete"):
        await mx.get_contract_info()
    await client.aclose()
