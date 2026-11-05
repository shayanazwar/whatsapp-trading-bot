from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from rslr.data.candles import CandleDataError, build_timeframes, validate_candles


def candles(hours: int = 48) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(hours):
        price = 100 + i
        rows.append({
            "timestamp": start + timedelta(hours=i),
            "open": price,
            "high": price + 2,
            "low": price - 1,
            "close": price + 1,
            "volume": 10,
        })
    return pd.DataFrame(rows)


def test_timeframe_builder_constructs_4h_12h_1d_from_complete_children():
    output = build_timeframes(candles(48))
    assert len(output["1H"]) == 48
    assert len(output["4H"]) == 12
    assert len(output["12H"]) == 4
    assert len(output["1D"]) == 2
    first = output["12H"].iloc[0]
    assert first.open == 100
    assert first.close == 112
    assert first.high == 113
    assert first.low == 99


def test_missing_hour_invalidates_affected_aggregate_bars():
    frame = candles(48).drop(index=[3]).reset_index(drop=True)
    output = build_timeframes(frame)
    assert len(output["4H"]) == 11
    assert len(output["12H"]) < 4


def test_duplicate_timestamps_rejected():
    frame = candles(4)
    frame.loc[3, "timestamp"] = frame.loc[2, "timestamp"]
    with pytest.raises(CandleDataError, match="Duplicate"):
        validate_candles(frame)


def test_bad_ohlc_rejected():
    frame = candles(3)
    frame.loc[1, "high"] = frame.loc[1, "low"] - 1
    with pytest.raises(CandleDataError, match="OHLC invalid"):
        validate_candles(frame)
