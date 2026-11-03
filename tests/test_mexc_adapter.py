from unittest.mock import Mock
import pandas as pd
from app.market.mexc import MexcPublicClient


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
