from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.market.archive import CandleArchive
from app.market.candles import Candle, aggregate_3m_from_1m


def _minute_bars(symbol="BTC_USDT", start=1_704_067_200):
    # start is UTC/epoch aligned to a 3-minute boundary
    return [
        Candle(
            symbol, "1M", start + i * 60,
            100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i,
            volume=10.0 + i, amount=(10.0 + i) * (100.5 + i),
        )
        for i in range(6)
    ]


def test_archive_append_is_idempotent_and_round_trips_both_timeframes(tmp_path):
    archive = CandleArchive(tmp_path / "market")
    source = _minute_bars()
    assert archive.append(source) == 6
    assert archive.append(source) == 0

    derived = aggregate_3m_from_1m(source)
    assert len(derived) == 2
    assert archive.append(derived) == 2
    assert archive.load("BTCUSDT", source[0].open_time, source[-1].open_time, "1M") == source
    assert archive.load("BTCUSDT", derived[0].open_time, derived[-1].open_time, "3M") == derived
    assert archive.latest_open_time("BTCUSDT", "1M") == source[-1].open_time
    assert archive.latest_open_time("BTCUSDT", "3M") == derived[-1].open_time


def test_archive_rejects_conflicting_duplicate_candle(tmp_path):
    archive = CandleArchive(tmp_path / "market")
    source = _minute_bars()
    archive.append(source)
    with pytest.raises(ValueError, match="Conflicting archived"):
        archive.append([replace(source[0], close=source[0].close + 1.0)])


def test_archive_coverage_uses_utc_aligned_bar_boundaries(tmp_path):
    archive = CandleArchive(tmp_path / "market")
    source = _minute_bars()
    archive.append(source)
    result = archive.coverage("BTC_USDT", source[0].open_time + 1, source[-1].open_time + 59, "1M")
    assert result["candles"] == 5
    assert result["expected"] == 5
    assert result["coverage_pct"] == 100.0


def test_archive_rejects_unsupported_timeframe(tmp_path):
    archive = CandleArchive(tmp_path / "market")
    with pytest.raises(ValueError, match="Unsupported archive timeframe"):
        archive.latest_open_time("BTC_USDT", "15M")


def test_archive_hot_shard_cache_is_limited_to_today_and_yesterday(tmp_path):
    archive = CandleArchive(tmp_path / "market")
    today = datetime.now(timezone.utc).date()
    old_day = (today - timedelta(days=3)).isoformat()
    today_key = ("BTC_USDT", "1M", today.isoformat())
    old_key = ("BTC_USDT", "1M", old_day)
    archive._known_by_shard[old_key] = {}
    archive._known_by_shard[today_key] = {}
    current_open = int(datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    archive.append([Candle("BTC_USDT", "1M", current_open, 100, 101, 99, 100)])
    assert old_key not in archive._known_by_shard
    assert today_key in archive._known_by_shard
