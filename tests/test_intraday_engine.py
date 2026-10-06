from __future__ import annotations

import pytest

import app.analysis.engine as engine
from app.analysis.engine import calculate_trade_levels, evaluate_confirmation_families
from app.backtest.simulator import simulate_trade
from app.backtest.report import summarize


def candle(ts: int, o: float, h: float, l: float, c: float, v: float = 1000.0):
    return [ts, o, h, l, c, v]


def test_approved_signal_timeframes_are_exact_and_no_lower_timeframe_engine_gate_exists():
    assert engine.APPROVED_TIMEFRAMES == ("1D", "12H", "4H", "1H")
    source = open(engine.__file__, encoding="utf-8").read().upper()
    assert "5M" not in source
    assert "15M" not in source


def test_direction_alignment_uses_side_specific_votes_and_allows_recent_bos_in_range():
    daily = {"bull": False, "bear": False}
    bias = {"bull": True, "bear": False, "bull_votes": 4, "bear_votes": 1}
    assert engine._direction_aligned("LONG", daily, bias, "RANGE") is True
    assert engine._direction_aligned("SHORT", daily, bias, "RANGE") is False

    neutral = {"bull": False, "bear": False, "bull_votes": 1, "bear_votes": 1}
    assert engine._direction_aligned("LONG", daily, neutral, "RANGE", {"strength": 0.8}) is True
    assert engine._direction_aligned("SHORT", daily, neutral, "RANGE", {"strength": 0.8}) is True


def test_1h_trigger_accepts_recent_qualifying_bar_within_three_bar_window(monkeypatch):
    base = 1_700_000_000_000
    rows = [candle(base + i * 3_600_000, 100, 100.4, 99.6, 100.1) for i in range(50)]
    rows[47] = candle(base + 47 * 3_600_000, 100.0, 100.2, 99.7, 100.1)
    rows[48] = candle(base + 48 * 3_600_000, 100.2, 101.4, 100.1, 101.2, 1500)
    rows[49] = candle(base + 49 * 3_600_000, 101.1, 101.5, 100.8, 101.3, 1500)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 55.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 1.10)

    # retest_time is the OPEN of the 4H retest candle. The trigger must only
    # be evaluated after that entire 4H candle has closed.
    retest_open = base + 44 * 3_600_000
    result = engine._one_hour_trigger_confirmation(
        engine.convert_candles(rows), "LONG", 100.0, retest_time=retest_open
    )
    assert result["ready"] is True
    assert result["trigger_type"] in {"BREAKOUT", "RECLAIM"}
    assert 1 <= result["bars_after_retest"] <= engine.MAX_TRIGGER_BARS_1H


def test_1h_trigger_does_not_use_bar_inside_open_4h_retest(monkeypatch):
    base = 1_700_000_000_000
    rows = [candle(base + i * 3_600_000, 100, 100.4, 99.6, 100.1) for i in range(60)]
    # This bar sits inside the 4H retest candle and would qualify if the
    # trigger incorrectly used the retest OPEN as its causal boundary.
    rows[48] = candle(base + 48 * 3_600_000, 100.2, 101.5, 100.1, 101.3, 2000)
    rows[49] = candle(base + 49 * 3_600_000, 101.2, 101.6, 100.8, 101.4, 2000)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 55.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 1.20)

    result = engine._one_hour_trigger_confirmation(
        engine.convert_candles(rows),
        "LONG",
        100.0,
        retest_time=base + 48 * 3_600_000,
    )
    assert result["ready"] is False
    assert result["candle_time"] >= base + 52 * 3_600_000
    assert result["candle_time"] not in {base + 48 * 3_600_000, base + 49 * 3_600_000}


def test_1h_trigger_rejects_weak_execution_bar(monkeypatch):
    base = 1_700_000_000_000
    rows = [candle(base + i * 3_600_000, 100, 100.4, 99.6, 100.1) for i in range(50)]
    rows[-1] = candle(base + 49 * 3_600_000, 100.0, 100.6, 99.9, 100.2, 500)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 55.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 0.40)
    result = engine._one_hour_trigger_confirmation(
        engine.convert_candles(rows), "LONG", 100.0, retest_time=base + 48 * 3_600_000
    )
    assert result["ready"] is False


def test_structural_stop_is_symmetric_and_buffered():
    long = calculate_trade_levels({
        "setup": "LONG", "price": 100.0, "atr_1h": 0.5, "atr_4h": 1.0,
        "retest": {"low": 99.5, "high": 101.0}, "protected_low": 98.5,
        "target_frames": [],
    })
    short = calculate_trade_levels({
        "setup": "SHORT", "price": 100.0, "atr_1h": 0.5, "atr_4h": 1.0,
        "retest": {"low": 99.0, "high": 100.5}, "protected_high": 101.5,
        "target_frames": [],
    })
    assert long["stop_source"].startswith("4H protected")
    assert short["stop_source"].startswith("4H protected")
    assert abs(100 - long["stop_loss"]) == pytest.approx(abs(short["stop_loss"] - 100))
    assert long["sl_atr"] >= engine.MIN_SL_ATR
    assert short["sl_atr"] >= engine.MIN_SL_ATR


def test_target_path_uses_nearest_confirmed_htf_level_that_meets_geometry(monkeypatch):
    monkeypatch.setattr(engine, "_collect_structural_levels", lambda *args, **kwargs: [
        {"price": 101.0, "timeframe": "4H", "index": 1, "kind": "RESISTANCE"},
        {"price": 104.5, "timeframe": "12H", "index": 2, "kind": "RESISTANCE"},
        {"price": 108.0, "timeframe": "1D", "index": 3, "kind": "RESISTANCE"},
    ])
    levels = calculate_trade_levels({
        "setup": "LONG", "price": 100.0, "atr_1h": 1.0, "atr_4h": 1.0,
        "retest": {"low": 99.0}, "protected_low": 99.0,
        "target_frames": [("1D", []), ("12H", []), ("4H", [])],
    })
    assert levels["target_path_structural"] is True
    assert levels["tp"] == pytest.approx(104.5)
    assert levels["rr"] >= engine.MIN_RR


def test_target_path_fails_without_a_real_higher_timeframe_target(monkeypatch):
    monkeypatch.setattr(engine, "_collect_structural_levels", lambda *args, **kwargs: [
        {"price": 101.0, "timeframe": "1H", "index": 1, "kind": "RESISTANCE"},
        {"price": 101.5, "timeframe": "4H", "index": 2, "kind": "RESISTANCE"},
    ])
    result = engine._target_path([("1D", []), ("12H", []), ("4H", []), ("1H", [])], "LONG", 100.0, 99.0, 1.0)
    assert result["ok"] is False
    assert result["structural"] is False


def test_quality_score_is_supporting_evidence_only_and_stays_bounded():
    score, groups = engine._build_score(
        structure_quality=0.8,
        trigger_quality=0.7,
        momentum_quality=0.6,
        volume_quality=0.5,
        volatility_quality=1.0,
        location_quality=0.9,
        risk_quality=0.8,
    )
    assert 0 <= score <= 100
    assert score == sum(groups.values())
    assert set(groups) == {"structure_quality", "entry_quality", "momentum", "volume", "volatility", "location", "risk_geometry"}
    # Mandatory gates are intentionally absent from the score API.


def test_confirmation_families_are_diverse_supporting_evidence():
    out = evaluate_confirmation_families({
        "setup": "LONG",
        "momentum_quality": 0.8,
        "rvol_1h": 1.1,
        "atr_percentile": 50,
        "target_path_structural": True,
        "rolling_vwap_12h": 99.0,
        "price": 100.0,
        "structure_quality": 0.8,
        "trigger_quality": 0.7,
    })
    assert out["available"] == 7
    assert out["passed"] >= 4
    assert out["diversity_ok"] is True


def test_risk_constants_are_consistent_across_engine_and_risk_manager():
    from app.automation import risk_manager
    from app.automation import setup_filter
    assert engine.MIN_RR == pytest.approx(risk_manager.MIN_RR) == pytest.approx(2.0)
    assert engine.MIN_TP_ATR == pytest.approx(risk_manager.MIN_TP_ATR) == pytest.approx(setup_filter.MIN_TP_ATR)


def test_simulator_same_bar_rule_is_deterministic():
    signal = {"symbol": "TEST_USDT", "setup": "LONG", "entry": 100.0, "stop_loss": 95.0, "tp": 110.0}
    future = [candle(3_600_000, 100.0, 111.0, 94.0, 100.0)]
    conservative = simulate_trade(signal, future, signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0, same_bar_rule="SL_FIRST")
    permissive = simulate_trade(signal, future, signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0, same_bar_rule="TP_FIRST")
    assert conservative is not None and conservative.outcome == "SL"
    assert permissive is not None and permissive.outcome == "TP"


def test_report_tracks_expectancy_and_drawdown():
    a = simulate_trade({"symbol": "A", "setup": "LONG", "entry": 100, "stop_loss": 95, "tp": 110}, [candle(3_600_000, 100, 111, 100, 110)], signal_close_time_ms=0, fee_rate=0, slippage_bps=0)
    b = simulate_trade({"symbol": "B", "setup": "SHORT", "entry": 100, "stop_loss": 105, "tp": 90}, [candle(3_600_000, 100, 106, 94, 100)], signal_close_time_ms=0, fee_rate=0, slippage_bps=0)
    summary = summarize(days=1, period_start_ms=0, period_end_ms=86_400_000, coins_selected=2, coins_tested=2, data_errors=0, execution_errors=0, rejected_setups=0, trades=[x for x in (a, b) if x])
    assert summary.expectancy_r is not None
    assert summary.max_drawdown_r >= 0
