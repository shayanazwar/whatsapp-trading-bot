import pytest

from app.market.candles import Candle, CandleDataError, aggregate_12h_from_4h, closed_candles, parse_kline_payload, validate_contiguous
from tests.helpers import candles


def test_parse_mexc_array_payload():
    payload = {"success": True, "code": 0, "data": {
        "time": [1704067200, 1704070800],
        "open": [100, 101], "high": [102, 103], "low": [99, 100], "close": [101, 102],
        "vol": [12, 13], "amount": [1200, 1300]
    }}
    parsed = parse_kline_payload(payload, "BTC_USDT", "1H")
    assert len(parsed) == 2
    assert parsed[0].open_time == 1704067200
    assert parsed[1].close == 102
    assert parsed[0].close_time == 1704070800


def test_parse_detects_ohlc_invariant_failure():
    payload = {"data": {"time": [1704067200], "open": [100], "high": [90], "low": [95], "close": [98], "vol": [1]}}
    with pytest.raises(CandleDataError):
        parse_kline_payload(payload, "BTC_USDT", "1H")


def test_closed_candles_excludes_open_or_grace_window():
    data = candles("BTC_USDT", "1H", 1704067200, 3, 3600)
    # At time 2h + 5s only first bar has closed beyond the grace.
    result = closed_candles(data, 1704067200 + 2*3600 + 5, grace_seconds=5)
    assert [x.open_time for x in result] == [1704067200, 1704070800]


def test_12h_aggregation_requires_three_completed_aligned_4h_bars():
    bars = candles("BTC_USDT", "4H", 1704067200, 6, 14400, trend=1.0)
    result = aggregate_12h_from_4h(bars)
    assert len(result) == 2
    assert result[0].open_time == 1704067200
    assert result[0].open == bars[0].open
    assert result[0].close == bars[2].close
    assert result[0].high == max(x.high for x in bars[:3])
    assert result[0].low == min(x.low for x in bars[:3])
    assert result[0].volume == sum(x.volume for x in bars[:3])


def test_12h_aggregation_drops_missing_constituent_and_rejects_duplicates():
    bars = candles("BTC_USDT", "4H", 1704067200, 6, 14400)
    missing = [x for i, x in enumerate(bars) if i != 1]
    aggregated_missing = aggregate_12h_from_4h(missing)
    assert len(aggregated_missing) == 1
    assert aggregated_missing[0].open_time == bars[3].open_time
    duplicate = bars + [bars[0]]
    with pytest.raises(CandleDataError):
        aggregate_12h_from_4h(duplicate)


def test_12h_aggregation_does_not_assume_wrong_utc_alignment():
    misaligned = candles("BTC_USDT", "4H", 1704067200 + 3600, 6, 14400)
    assert aggregate_12h_from_4h(misaligned) == []


def test_contiguous_validator_detects_gap():
    bars = candles("BTC_USDT", "1H", 1704067200, 4, 3600)
    with pytest.raises(CandleDataError):
        validate_contiguous([bars[0], bars[2], bars[3]])


def test_timestamp_milliseconds_normalized():
    payload = {"data": {"time": [1704067200000], "open": [100], "high": [102], "low": [99], "close": [101], "vol": [12]}}
    parsed = parse_kline_payload(payload, "BTC_USDT", "1H")
    assert parsed[0].open_time == 1704067200
