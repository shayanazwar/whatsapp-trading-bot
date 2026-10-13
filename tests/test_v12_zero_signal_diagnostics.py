from __future__ import annotations

from app.analysis.patterns import confirm_triangle_breakout, detect_triangle_pattern
from app.backtest.report import summarize, format_report
from app.backtest.runner import _validated_candidate_side


def _candle(ts: int, price: float = 100.0):
    return {
        "time": ts,
        "open": price,
        "high": price + 1.0,
        "low": price - 1.0,
        "close": price,
        "volume": 1000.0,
    }


def test_triangle_detector_exposes_stable_rejection_reason():
    rows = [_candle(i * 14_400_000) for i in range(64)]
    diagnostics = {}
    assert detect_triangle_pattern(rows, atr_4h=1.0, diagnostics=diagnostics) is None
    assert diagnostics["reason"] == "INSUFFICIENT_CONFIRMED_PIVOTS"


def test_breakout_trigger_exposes_insufficient_history_reason():
    diagnostics = {}
    result = confirm_triangle_breakout(
        [], [], {}, "LONG", atr_1h=1.0, diagnostics=diagnostics
    )
    assert result is None
    assert diagnostics["reason"] == "INSUFFICIENT_1H_TRIGGER_HISTORY"


def test_backtest_candidate_side_accepts_both_directions():
    assert _validated_candidate_side({"setup": "LONG"}) == "LONG"
    assert _validated_candidate_side({"setup_candidate": "SHORT"}) == "SHORT"
    assert _validated_candidate_side({"setup": "FLAT"}) is None
    assert _validated_candidate_side({}) is None


def test_zero_signal_report_includes_architecture_and_top_failure_causes():
    summary = summarize(
        days=7,
        period_start_ms=1_790_697_600_000,
        period_end_ms=1_791_302_400_000,
        coins_selected=200,
        coins_tested=200,
        data_errors=0,
        execution_errors=0,
        rejected_setups=100,
        trades=[],
        diagnostics={
            "FIRST_FAILURE_LONG_TOTAL": 100,
            "FIRST_FAILURE_LONG_TRIANGLE_PATTERN_INSUFFICIENT_CONFIRMED_PIVOTS": 70,
            "FIRST_FAILURE_LONG_BREAKOUT_TRIGGER_BREAKOUT_RELATIVE_VOLUME_TOO_LOW": 30,
            "FIRST_FAILURE_SHORT_TOTAL": 100,
            "FIRST_FAILURE_SHORT_TRIANGLE_PATTERN_INSUFFICIENT_CONFIRMED_PIVOTS": 80,
            "FIRST_FAILURE_SHORT_BREAKOUT_TRIGGER_NO_RECENT_BOUNDARY_CROSS": 20,
        },
        engine_config={"setup_architecture": "TRIANGLE_BREAKOUT"},
    )
    text = format_report(summary)
    assert "Setup Architecture: TRIANGLE_BREAKOUT" in text
    assert "TOP REJECTION CAUSES" in text
    assert "Insufficient Confirmed Pivots: 70" in text
    assert "No Recent Boundary Cross: 20" in text
