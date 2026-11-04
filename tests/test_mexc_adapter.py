from unittest.mock import Mock
import pandas as pd
import pytest
from app.market.mexc import MexcAPIError, MexcPublicClient


def test_klines_parses_documented_array_response():
    response = Mock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {"success":True,"data":{
        "time":[1767225600],"open":[100],"high":[102],"low":[99],"close":[101],"vol":[50],"amount":[5000]
    }}
    response.raise_for_status.return_value = None
    http = Mock()
    http.get.return_value = response
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0
    # Use an isolated single-window request range.
    frame = client.klines("BTC_USDT", "Min60", 1767225600, 1767229200)
    assert len(frame) == 1
    assert frame.iloc[0].close == 101
    client.close()


def test_funding_history_range_paginates_and_filters():
    def response(data):
        r = Mock()
        r.status_code = 200
        r.headers = {}
        r.json.return_value = {"success": True, "data": data}
        r.raise_for_status.return_value = None
        return r

    http = Mock()
    http.get.side_effect = [
        response({"totalPage": 2, "resultList": [
            {"settleTime": 3000, "fundingRate": 0.001},
            {"settleTime": 2000, "fundingRate": 0.002},
        ]}),
        response({"totalPage": 2, "resultList": [
            {"settleTime": 1000, "fundingRate": -0.001},
        ]}),
    ]
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0
    events = client.funding_history_range("BTC_USDT", 1500, 3500, page_size=1000)
    assert [int(x["settleTime"]) for x in events] == [2000, 3000]
    assert http.get.call_count == 2
    client.close()


def test_contract_detail_country_uses_current_official_futures_endpoint_and_optional_symbol():
    response = Mock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {"success": True, "data": [{"symbol": "BTC_USDT"}]}
    response.raise_for_status.return_value = None
    http = Mock()
    http.get.return_value = response
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0

    contracts = client.contracts("BTC_USDT")

    assert contracts == [{"symbol": "BTC_USDT"}]
    args, kwargs = http.get.call_args
    assert args[0] == "https://api.mexc.com/api/v1/contract/detail/country"
    assert kwargs["params"] == {"symbol": "BTC_USDT"}
    client.close()


def test_contract_detail_country_bulk_response_uses_current_futures_endpoint():
    response = Mock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {"success": True, "data": [{"symbol": "BTC_USDT"}, {"symbol": "ETH_USDT"}]}
    response.raise_for_status.return_value = None
    http = Mock()
    http.get.return_value = response
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0

    contracts = client.contracts()

    assert [item["symbol"] for item in contracts] == ["BTC_USDT", "ETH_USDT"]
    args, kwargs = http.get.call_args
    assert args[0] == "https://api.mexc.com/api/v1/contract/detail/country"
    assert kwargs.get("params") is None
    client.close()


def test_funding_history_stops_when_first_page_already_covers_requested_window():
    def response(data):
        r = Mock()
        r.status_code = 200
        r.headers = {}
        r.json.return_value = {"success": True, "data": data}
        r.raise_for_status.return_value = None
        return r

    http = Mock()
    http.get.return_value = response({
        "totalPage": 100,
        "resultList": [
            {"settleTime": 3000, "fundingRate": 0.001},
            {"settleTime": 2000, "fundingRate": 0.002},
            {"settleTime": 1000, "fundingRate": 0.003},
        ],
    })
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0
    events = client.funding_history_range("BTC_USDT", 1500, 3500, page_size=1000, max_pages=10)
    assert [int(x["settleTime"]) for x in events] == [2000, 3000]
    assert http.get.call_count == 1
    client.close()


def test_funding_history_fails_if_page_cap_does_not_cover_requested_window():
    def response(page_num):
        r = Mock()
        r.status_code = 200
        r.headers = {}
        r.json.return_value = {"success": True, "data": {
            "totalPage": 20,
            "resultList": [{"settleTime": 5000 - page_num, "fundingRate": 0.001}],
        }}
        r.raise_for_status.return_value = None
        return r

    http = Mock()
    http.get.side_effect = [response(1), response(2)]
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0
    with pytest.raises(MexcAPIError, match="not fully covered"):
        client.funding_history_range("BTC_USDT", 1000, 6000, page_size=1, max_pages=2)
    assert http.get.call_count == 2
    client.close()
