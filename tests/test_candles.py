from datetime import datetime, timezone
import pandas as pd
import pytest
from app.market.candles import validate_candles, closed_candles, aggregate_12h_from_4h


def test_validates_sorts_and_deduplicates():
    frame = pd.DataFrame([
        {"timestamp":"2026-01-01T01:00:00Z","open":10,"high":12,"low":9,"close":11,"volume":5,"amount":55},
        {"timestamp":"2026-01-01T00:00:00Z","open":9,"high":11,"low":8,"close":10,"volume":4,"amount":40},
        {"timestamp":"2026-01-01T01:00:00Z","open":10,"high":13,"low":9,"close":12,"volume":6,"amount":66},
    ])
    out = validate_candles(frame)
    assert len(out) == 2
    assert out.iloc[0].timestamp < out.iloc[1].timestamp
    assert out.iloc[-1].close == 12


def test_rejects_invalid_ohlc():
    frame = pd.DataFrame([{"timestamp":"2026-01-01T00:00:00Z","open":10,"high":9,"low":8,
                           "close":11,"volume":1,"amount":1}])
    with pytest.raises(ValueError, match="Invalid candle high"):
        validate_candles(frame)


def test_closed_candles_excludes_incomplete():
    frame = pd.DataFrame([{"timestamp":"2026-01-01T00:00:00Z","open":10,"high":11,"low":9,
                           "close":10,"volume":1,"amount":1}])
    assert len(closed_candles(frame, 3600, datetime(2026,1,1,0,59,tzinfo=timezone.utc))) == 0
    assert len(closed_candles(frame, 3600, datetime(2026,1,1,1,0,tzinfo=timezone.utc))) == 1


def test_12h_requires_three_contiguous_utc_aligned_4h(candles_4h):
    result = aggregate_12h_from_4h(candles_4h)
    assert len(result) == 30
    assert result.iloc[0].timestamp.hour == 0
    broken = candles_4h.drop(index=1)
    result2 = aggregate_12h_from_4h(broken)
    assert len(result2) < len(result)
