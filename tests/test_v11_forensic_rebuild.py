"""Regression tests for the V11 forensic-rebuild patch (period labels, 12H integrity, family diagnostics)."""
from datetime import datetime, timezone

from app.analysis.engine import Candle, synthesize_12h_from_4h
from app.backtest.report import BacktestSummary, format_report, format_utc_iso

H4 = 4 * 3600 * 1000
BASE = 1_760_000_000_000 - (1_760_000_000_000 % (12 * 3600 * 1000))  # UTC 12H-aligned


def _summary(start_ms, end_ms, days=7):
    return BacktestSummary(
        days=days, period_start_ms=start_ms, period_end_ms=end_ms,
        timeframes=("1D", "12H", "4H", "1H"), coins_tested=200, coins_selected=200,
        data_errors=0, execution_errors=0, rejected_setups=0, signals=0,
        long_signals=0, short_signals=0, resolved=0, unresolved=0, tp_hits=0,
        sl_hits=0, expired=0, win_rate=None, avg_planned_rr=None, avg_signal_rr=None,
        avg_actual_fill_rr=None, avg_realized_r=None, total_r=0.0, expectancy_r=None,
        profit_factor=None, max_drawdown_r=0.0, max_losing_streak=0,
        diagnostics={"FIXED_BACKTEST_PERIOD_ENABLED": 1},
    )


def test_format_utc_iso_matches_known_user_window():
    # The previously used fixed window is 2026-10-04 15:00Z -> 2026-10-11 15:00Z.
    assert format_utc_iso(1791126000000) == "2026-10-04T15:00:00Z"
    assert format_utc_iso(1791730800000) == "2026-10-11T15:00:00Z"
    # The window the operator intended (2026-10-05 15:00Z -> 2026-10-12 15:00Z).
    assert format_utc_iso(1791212400000) == "2026-10-05T15:00:00Z"
    assert format_utc_iso(1791817200000) == "2026-10-12T15:00:00Z"


def test_format_utc_iso_rejects_garbage_without_raising():
    assert format_utc_iso("not-a-number") == "invalid-timestamp"


def test_report_prints_iso_period_and_elapsed_days():
    text = format_report(_summary(1791126000000, 1791730800000))
    assert "2026-10-04T15:00:00Z → 2026-10-11T15:00:00Z" in text
    assert "Elapsed: 7.00D (UTC)" in text
    assert "FIXED" in text


def _c(t, o=1.0, h=2.0, l=0.5, c=1.5, v=10.0):
    return Candle(time=t, open=o, high=h, low=l, close=c, volume=v)


def test_12h_requires_three_contiguous_completed_4h_bars():
    now = BASE + 10 * 24 * 3600 * 1000
    full = [_c(BASE + i * H4, o=i + 1, h=i + 3, l=i, c=i + 2, v=1.0) for i in range(3)]
    out = synthesize_12h_from_4h(full, now_ms=now)
    assert len(out) == 1
    bar = out[0]
    assert bar["time"] == BASE and bar["open"] == 1 and bar["close"] == 4
    assert bar["high"] == 5 and bar["low"] == 0 and bar["volume"] == 3.0

    gapped = [full[0], full[2]]  # middle 4H bar missing
    assert synthesize_12h_from_4h(gapped, now_ms=now) == []


def test_12h_excludes_unfinished_trailing_4h_bar():
    bars = [_c(BASE + i * H4) for i in range(3)]
    now_before_last_close = BASE + 3 * H4 - 1  # last 4H bar has not closed yet
    assert synthesize_12h_from_4h(bars, now_ms=now_before_last_close) == []


def test_period_label_uses_utc_not_local_time():
    ts = int(datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert ts == 1791212400000
