from __future__ import annotations

from app.analysis.smc_engine import (
    _net_rr,
    _liquidity_trigger,
    _swing_points,
    _zones,
    analyze_candles,
    synthesize_12h_from_4h_smc,
)
from app.backtest.runner import SUPPORTED_BACKTEST_DAYS
from app.config import Settings

HOUR = 3_600_000
FOUR_HOUR = 4 * HOUR
DAY = 24 * HOUR


def candle(t, o, h, l, c, v=100.0):
    return {"time": int(t), "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)}


def test_swing_is_unavailable_until_right_confirmation_bars_close():
    base = 1_800_000_000_000
    rows = [candle(base + i * FOUR_HOUR, 10, h, l, 10) for i, (h, l) in enumerate(
        [(11, 9), (12, 8), (15, 7), (12, 8), (11, 9)]
    )]
    highs_before, _ = _swing_points(rows[:4])
    highs_after, _ = _swing_points(rows)
    assert not highs_before
    assert highs_after[-1]["index"] == 2
    assert highs_after[-1]["known_index"] == 4


def test_long_and_short_sweep_require_a_later_reclaim_candle():
    base = 1_800_000_000_000
    long_rows = [candle(base + i * HOUR, 99, 100, 98, 99) for i in range(7)]
    long_rows[5] = candle(base + 5 * HOUR, 98.2, 98.5, 97.0, 97.5)
    long_rows[6] = candle(base + 6 * HOUR, 98.2, 100.0, 97.9, 99.8)
    long = _liquidity_trigger(long_rows, "LONG")
    assert long is not None and long["sweep_idx"] == 5 and long["reclaim_idx"] == 6

    short_rows = [candle(base + i * HOUR, 101, 102, 100, 101) for i in range(7)]
    short_rows[5] = candle(base + 5 * HOUR, 101.8, 103.0, 101.5, 102.5)
    short_rows[6] = candle(base + 6 * HOUR, 101.8, 102.1, 100.0, 100.2)
    short = _liquidity_trigger(short_rows, "SHORT")
    assert short is not None and short["sweep_idx"] == 5 and short["reclaim_idx"] == 6


def test_bullish_fvg_lifecycle_is_measured_from_completed_candles():
    base = 1_800_000_000_000
    rows = [
        candle(base, 100, 101, 99, 100),
        candle(base + FOUR_HOUR, 101, 103, 100.5, 102.5),
        candle(base + 2 * FOUR_HOUR, 103, 105, 102, 104),
        candle(base + 3 * FOUR_HOUR, 103, 104, 101.5, 103),
    ]
    zones = _zones(rows, "LONG")
    fvg = next(z for z in zones if z["kind"] == "BULLISH_FVG")
    assert fvg["low"] == 101.0
    assert fvg["high"] == 102.0
    assert fvg["status"] == "PARTIALLY_MITIGATED"


def test_12h_aggregator_requires_three_contiguous_utc_aligned_4h_candles():
    base = 1_800_000_000_000
    utc_midnight = (base // DAY) * DAY
    rows = [candle(utc_midnight + i * FOUR_HOUR, 100 + i, 102 + i, 99 + i, 101 + i, 10 + i) for i in range(9)]
    # now_ms is the close of the final 4H candle; data spans a UTC day boundary.
    aggregated = synthesize_12h_from_4h_smc(rows, now_ms=utc_midnight + 36 * HOUR)
    assert len(aggregated) == 3
    assert aggregated[0]["time"] == utc_midnight
    assert aggregated[0]["open"] == rows[0]["open"]
    assert aggregated[0]["high"] == max(x["high"] for x in rows[:3])
    assert aggregated[0]["low"] == min(x["low"] for x in rows[:3])
    assert aggregated[0]["close"] == rows[2]["close"]
    assert aggregated[0]["volume"] == sum(x["volume"] for x in rows[:3])
    assert aggregated[2]["time"] == utc_midnight + DAY
    assert aggregated[2]["open"] == rows[6]["open"]
    missing = rows[:2] + rows[3:]
    partial = synthesize_12h_from_4h_smc(missing, now_ms=utc_midnight + 36 * HOUR)
    assert all(int(x["time"]) != utc_midnight for x in partial)


def test_net_rr_accounts_for_round_trip_costs_and_invalid_risk():
    assert _net_rr(10, 5, 100, 0.001) < 2.0
    assert _net_rr(1, 0, 100, 0.001) == 0.0


def test_engine_returns_no_trade_for_insufficient_history_not_a_fake_signal():
    result = analyze_candles("TEST_USDT", [], None, [], [], now_ms=1_800_000_000_000)
    assert result["setup"] == "NO TRADE"
    assert result["technical_candidate"] is False
    assert "Insufficient closed data" in result["primary_rejection_reason"]


def test_3d_backtest_window_is_supported_and_live_trading_defaults_off():
    assert {1, 3, 7, 30}.issubset(SUPPORTED_BACKTEST_DAYS)
    settings = Settings()
    assert settings.analysis_engine == "V11"
    assert settings.auto_trade_enabled is False
    assert settings.allow_live_execution is False


def _synthetic_directional_history():
    """Deterministic multi-TF tape with a known 1H sweep/reclaim on both sides."""
    import math
    from app.analysis.engine import convert_candles

    now = (1_800_000_000_000 // FOUR_HOUR) * FOUR_HOUR
    daily = []
    for i in range(250):
        p = 107 + 0.06 * i + 0.8 * math.sin(i / 2.5)
        daily.append(candle(now - 250 * DAY + i * DAY, p - 0.1, p + 0.25, p - 0.25, p))
    four = []
    for i in range(220):
        p = 100 + 0.06 * i + 2.5 * math.sin(i / 5)
        four.append(candle(now - 220 * FOUR_HOUR + i * FOUR_HOUR, p - 0.2, p + 0.8, p - 0.8, p))
    forced_4h = {
        212: (118.7, 119.5, 118.5, 119.0), 213: (119.0, 119.8, 118.6, 119.3),
        214: (119.3, 120.0, 118.7, 119.2), 215: (119.2, 120.1, 118.8, 119.0),
        216: (119.0, 131.0, 118.6, 119.0), 217: (119.0, 120.0, 118.3, 119.9),
        218: (123.2, 123.7, 123.1, 123.4), 219: (123.0, 124.6, 122.7, 124.0),
    }
    for i, (o, h, l, close) in forced_4h.items():
        four[i].update(open=o, high=h, low=l, close=close)
    hourly = []
    for i in range(220):
        idx = 165 + i // 4
        p = 100 + 0.06 * idx + 2.5 * math.sin(idx / 5)
        hourly.append(candle(now - 220 * HOUR + i * HOUR, p - 0.1, p + 0.25, p - 0.25, p))
    forced_1h = {
        204: (119.0, 131.0, 118.6, 119.0), 208: (119.0, 120.0, 118.3, 119.2),
        209: (119.2, 119.8, 118.7, 119.6), 210: (119.6, 120.0, 119.0, 119.9),
        211: (119.9, 120.0, 119.2, 119.9), 212: (123.2, 123.6, 123.1, 123.4),
        213: (123.4, 123.7, 123.1, 123.4), 214: (123.4, 123.6, 123.1, 123.5),
        215: (123.5, 123.7, 123.1, 123.4), 216: (123.3, 123.6, 123.1, 123.4),
        217: (123.4, 123.6, 123.1, 123.3),
    }
    for i, (o, h, l, close) in forced_1h.items():
        hourly[i].update(open=o, high=h, low=l, close=close)
    hourly[-2] = candle(hourly[-2]["time"], 123.0, 123.3, 122.8, 122.9, 2000)
    hourly[-1] = candle(hourly[-1]["time"], 123.0, 124.6, 122.7, 124.0, 2500)
    daily = convert_candles(daily)
    four = convert_candles(four)
    hourly = convert_candles(hourly)
    twelve = synthesize_12h_from_4h_smc(four, now_ms=now)
    return now, daily, twelve, four, hourly


def _mirror_prices(rows, center=260.0):
    return [candle(
        r["time"], center - r["open"], center - r["low"], center - r["high"], center - r["close"], r["volume"]
    ) for r in rows]


def test_full_smc_engine_can_generate_causal_long_and_short_signals():
    now, daily, twelve, four, hourly = _synthetic_directional_history()
    long_result = analyze_candles("LONG_FIXTURE", daily, twelve, four, hourly, now_ms=now)
    assert long_result["setup"] == "LONG", long_result.get("primary_rejection_reason")
    assert long_result["stop_loss"] < long_result["entry"] < long_result["tp"]
    assert long_result["rr"] >= 1.60

    daily_s = _mirror_prices(daily)
    four_s = _mirror_prices(four)
    hourly_s = _mirror_prices(hourly)
    twelve_s = synthesize_12h_from_4h_smc(four_s, now_ms=now)
    short_result = analyze_candles("SHORT_FIXTURE", daily_s, twelve_s, four_s, hourly_s, now_ms=now)
    assert short_result["setup"] == "SHORT", short_result.get("primary_rejection_reason")
    assert short_result["tp"] < short_result["entry"] < short_result["stop_loss"]
    assert short_result["rr"] >= 1.60


def test_rolling_period_ends_at_latest_hour_boundary_not_72_hours_early(monkeypatch):
    from datetime import datetime, timezone
    from app.backtest import runner as runner_module

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 13, 21, 37, tzinfo=timezone.utc)

    monkeypatch.setattr(runner_module, "datetime", FixedDateTime)
    monkeypatch.delenv("BACKTEST_START_MS", raising=False)
    monkeypatch.delenv("BACKTEST_END_MS", raising=False)
    monkeypatch.delenv("V11_BACKTEST_START_MS", raising=False)
    monkeypatch.delenv("V11_BACKTEST_END_MS", raising=False)
    start, end = runner_module._period(3)
    expected_end = int(datetime(2026, 10, 13, 21, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert end == expected_end
    assert end - start == 3 * DAY


def test_current_partial_1h_candle_is_open_only_for_simulation():
    from app.backtest.runner import _simulation_safe_future

    base = 1_800_000_000_000
    rows = [
        candle(base, 100, 105, 99, 104),
        candle(base + HOUR, 104, 110, 103, 109),
        candle(base + 2 * HOUR, 109, 150, 80, 120),
    ]
    safe = _simulation_safe_future(rows, as_of_ms=base + 2 * HOUR + 15 * 60_000)
    assert len(safe) == 3
    assert safe[0]["high"] == 105 and safe[1]["low"] == 103
    assert safe[-1]["high"] == safe[-1]["low"] == safe[-1]["close"] == safe[-1]["open"] == 109


def test_duplicate_4h_timestamps_are_deduplicated_before_12h_aggregation():
    base = 1_800_000_000_000
    utc_midnight = (base // DAY) * DAY
    rows = [candle(utc_midnight + i * FOUR_HOUR, 100 + i, 102 + i, 99 + i, 101 + i, 10 + i) for i in range(3)]
    duplicate = candle(utc_midnight, 200, 300, 150, 250, 999)
    result = synthesize_12h_from_4h_smc(rows + [duplicate], now_ms=utc_midnight + 12 * HOUR)
    assert len(result) == 1
    assert result[0]["open"] == 200
    assert result[0]["high"] == 300
    assert result[0]["volume"] == 999 + sum(x["volume"] for x in rows[1:])


def test_control_engine_is_a_backtest_comparator_and_does_not_fake_signals():
    from app.analysis.control_engine import analyze_candles as analyze_control_candles

    settings = Settings(BACKTEST_ENGINE="CONTROL")
    result = analyze_control_candles("CONTROL_FIXTURE", [], None, [], [], now_ms=1_800_000_000_000)
    assert settings.backtest_engine == "CONTROL"
    assert result["setup"] == "NO TRADE"
    assert result["signal_engine_version"] == "CONTROL-1.0"


def test_scanner_default_strategy_and_live_execution_remain_off():
    settings = Settings()
    assert settings.analysis_engine == "V11"
    assert settings.auto_trade_enabled is False
    assert settings.allow_live_execution is False


def test_canonical_backtest_window_env_vars_are_honored_and_override_legacy(monkeypatch):
    from app.backtest.runner import _period

    start = 1_790_000_000_000
    end = start + 7 * DAY
    monkeypatch.setenv("BACKTEST_START_MS", str(start))
    monkeypatch.setenv("BACKTEST_END_MS", str(end))
    monkeypatch.setenv("V11_BACKTEST_START_MS", str(start - DAY))
    monkeypatch.setenv("V11_BACKTEST_END_MS", str(end + DAY))
    assert _period(30) == (start, end)
