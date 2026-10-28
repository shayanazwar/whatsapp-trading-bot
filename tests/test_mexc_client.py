import asyncio

import httpx
import pytest

from app.mexc.client import MexcClient, MexcRequestError


@pytest.mark.asyncio
async def test_client_uses_official_domain_and_parses_ticker():
    async def handler(request):
        assert request.url.host == "api.mexc.com"
        assert request.url.path == "/api/v1/contract/ticker"
        return httpx.Response(200, json={"success": True, "data": {"symbol": "BTC_USDT", "lastPrice": "67200.5"}})
    client0 = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MexcClient(client=client0, min_interval_by_family={"ticker": 0})
    data = await client.ticker("BTCUSDT")
    assert data["lastPrice"] == "67200.5"
    await client0.aclose()


@pytest.mark.asyncio
async def test_rate_limit_retry_after_http_date_does_not_crash():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
                json={"success": False, "code": 429, "message": "rate limited"},
            )
        return httpx.Response(
            200,
            json={"success": True, "data": {"symbol": "BTC_USDT", "lastPrice": "67000"}},
        )

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MexcClient(
        client=transport_client, max_retries=1, min_interval_by_family={"ticker": 0}
    )
    try:
        result = await client.ticker("BTCUSDT")
        assert result["lastPrice"] == "67000"
        assert calls == 2
    finally:
        await client.aclose()
        await transport_client.aclose()


@pytest.mark.asyncio
async def test_client_rejects_logical_success_false_even_http_200():
    async def handler(request):
        return httpx.Response(200, json={"success": False, "code": 1001, "message": "bad request"})
    client0 = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MexcClient(client=client0, max_retries=0, min_interval_by_family={"ticker": 0})
    with pytest.raises(MexcRequestError):
        await client.ticker("BTCUSDT")
    await client0.aclose()


@pytest.mark.asyncio
async def test_client_refuses_native_12h_request():
    client = MexcClient(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    with pytest.raises(ValueError, match="derive it"):
        await client.get_klines("BTCUSDT", "12H", now_epoch=1704067200)
    await client._client.aclose()


def test_client_rejects_lookalike_hosts():
    with pytest.raises(ValueError, match="exact official Futures host"):
        MexcClient(base_url="https://evil-mexc.com")
    with pytest.raises(ValueError, match="exact official Futures host"):
        MexcClient(base_url="https://api.mexc.com.attacker.invalid")


@pytest.mark.asyncio
async def test_contract_metadata_is_shared_across_concurrent_symbol_lookups():
    calls = 0

    async def handler(request):
        nonlocal calls
        assert request.url.path == "/api/v1/contract/detail"
        calls += 1
        return httpx.Response(200, json={"success": True, "data": [
            {"symbol": "BTC_USDT", "state": 0, "apiAllowed": True},
            {"symbol": "ETH_USDT", "state": 0, "apiAllowed": True},
        ]})

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MexcClient(client=transport_client)
    try:
        btc, eth = await asyncio.gather(
            client.contract_detail("BTCUSDT"), client.contract_detail("ETHUSDT")
        )
        assert btc is not None and btc["symbol"] == "BTC_USDT"
        assert eth is not None and eth["symbol"] == "ETH_USDT"
        assert calls == 1

        # Cached rows are copied: caller mutation cannot corrupt later symbol lookups.
        btc["state"] = 999
        refreshed = await client.contract_detail("BTCUSDT")
        assert refreshed is not None and refreshed["state"] == 0
        assert calls == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_contract_metadata_cache_expires():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"success": True, "data": [{"symbol": "BTC_USDT"}]})

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MexcClient(client=transport_client)
    client._contract_details_cache_ttl = 0.01
    try:
        await client.contract_details()
        await asyncio.sleep(0.02)
        await client.contract_details()
        assert calls == 2
    finally:
        await client.aclose()
