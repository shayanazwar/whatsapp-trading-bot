from datetime import datetime, timedelta, timezone

import httpx
import pytest

from rslr.exchange.mexc import MEXCRestClient


def test_kline_parser_maps_exchange_arrays_and_drops_incomplete_bars():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    completed = int((now - timedelta(hours=2)).timestamp())
    incomplete = int((now - timedelta(minutes=30)).timestamp())
    payload = {
        "success": True,
        "code": 0,
        "data": {
            "time": [completed, incomplete],
            "open": [100, 110], "high": [105, 115], "low": [95, 105],
            "close": [103, 112], "vol": [10, 12],
        },
    }

    def handler(request):
        assert request.url.path.endswith("/api/v1/contract/kline/BTC_USDT")
        assert request.url.params["interval"] == "Min60"
        return httpx.Response(200, json=payload)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = MEXCRestClient(client=http)
    start, end = now - timedelta(days=1), now
    frame = client.get_klines("BTC_USDT", start, end, "Min60")
    assert len(frame) == 1
    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    client.close()
    http.close()


def test_unsupported_lower_timeframe_is_rejected_before_request():
    client = MEXCRestClient(client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))))
    with pytest.raises(ValueError, match="Only 1H, 4H and 1D"):
        client.get_klines("BTC_USDT", datetime.now(timezone.utc), datetime.now(timezone.utc), "Min5")
    client.close()


def test_contract_metadata_maps_exchange_precision_and_cost_fields():
    from rslr.models import Contract
    contract = Contract.from_api({
        "symbol": "BTC_USDT", "baseCoin": "BTC", "quoteCoin": "USDT", "contractSize": "0.0001",
        "priceUnit": "0.1", "volUnit": "1", "minVol": "1", "takerFeeRate": "0.0002",
        "maxLeverage": 100, "state": 0,
    })
    assert contract.contract_size == 0.0001
    assert contract.price_tick == 0.1
    assert contract.quantity_step == 1
    assert contract.min_volume == 1
    assert contract.taker_fee_rate == 0.0002
