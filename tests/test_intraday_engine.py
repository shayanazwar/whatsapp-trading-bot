from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.analysis.engine as engine
from app.analysis.engine import calculate_trade_levels
from app.automation.scanner import MexcScanner
from app.backtest.report import summarize
from app.backtest.simulator import simulate_trade


def _candle(ts: int, o: float, h: float, l: float, c: float, v: float = 1000.0):
    return [ts, o, h, l, c, v]


def test_long_short_stop_geometry_is_symmetric_and_uses_deepest_invalidation():
    long = calculate_trade_levels({
        "setup": "LONG", "price": 100.0, "atr": 1.0,
        "retest": {"low": 99.5, "high": 101.0},
        "protected_low": 98.5,
        "support": 99.4,
        "target_frames": [],
    })
    short = calculate_trade_levels({
        "setup": "SHORT", "price": 100.0, "atr": 1.0,
        "retest": {"low": 99.0, "high": 100.5},
        "protected_high": 101.5,
        "resistance": 100.6,
        "target_frames": [],
    })
    assert long["stop_source"].startswith("DEEPEST(")
    assert short["stop_source"].startswith("DEEPEST(")
    assert long["stop_loss"] == pytest.approx(98.35)
    assert short["stop_loss"] == pytest.approx(101.65)
    assert abs(100 - long["stop_loss"]) == pytest.approx(abs(short["stop_loss"] - 100))


def test_structural_stop_uses_intraday_swing_floor(monkeypatch):
    monkeypatch.setattr(engine, "_collect_structural_levels", lambda *args, **kwargs: [
        {"price": 102.0, "timeframe": "15M", "index": 10, "kind": "RESISTANCE"},
        {"price": 106.5, "timeframe": "1H", "index": 20, "kind": "RESISTANCE"},
    ])
    levels = calculate_trade_levels({
        "setup": "LONG", "price": 100.0, "atr": 1.0,
        "retest": {"low": 99.55, "high": 100.5},
        "protected_low": 99.50,
        "target_frames": [("15M", []), ("1H", [])],
    })
    assert levels["stop_distance_pct"] >= engine.MIN_SL_ATR / 100.0
    assert levels["sl_atr"] >= engine.MIN_SL_ATR
    assert levels["trade_geometry_ok"] is True
    assert levels["tp"] == pytest.approx(106.5)


def test_target_path_does_not_skip_a_near_obstacle_and_requires_major_tp2(monkeypatch):
    monkeypatch.setattr(engine, "_collect_structural_levels", lambda *args, **kwargs: [
        {"price": 103.5, "timeframe": "15M", "index": 10, "kind": "RESISTANCE"},
        {"price": 106.5, "timeframe": "1H", "index": 20, "kind": "RESISTANCE"},
    ])
    levels = calculate_trade_levels({
        "setup": "LONG", "price": 100.0, "atr": 1.0,
        "retest": {"low": 98.5, "high": 101.0},
        "protected_low": 98.5,
        "target_frames": [("15M", []), ("1H", [])],
    })
    assert levels["trade_geometry_ok"] is True
    assert levels["tp"] == pytest.approx(106.5)
    assert levels["tp_distance_atr"] >= 2.5
    assert levels["rr"] >= 2.5


def test_structural_target_path_allows_far_tp2_when_supported(monkeypatch):
    monkeypatch.setattr(engine, "_collect_structural_levels", lambda *args, **kwargs: [
        {"price": 103.5, "timeframe": "15M", "index": 10, "kind": "RESISTANCE"},
        {"price": 106.5, "timeframe": "1H", "index": 20, "kind": "RESISTANCE"},
    ])
    levels = calculate_trade_levels({
        "setup": "LONG", "price": 100.0, "atr": 1.0,
        "retest": {"low": 98.5, "high": 101.0},
        "protected_low": 98.5,
        "target_frames": [("15M", []), ("1H", [])],
    })
    assert levels["trade_geometry_ok"] is True
    assert levels["tp"] == pytest.approx(106.5)
    assert levels["rr"] > 2.0


def test_15m_entry_confirmation_is_primary_and_mirrored(monkeypatch):
    candles = [_candle(i * 900_000, 100.0, 100.4, 99.6, 100.1) for i in range(30)]
    candles[-2] = _candle(28 * 900_000, 100.2, 101.0, 99.9, 100.5)
    candles[-1] = _candle(29 * 900_000, 100.6, 103.0, 100.4, 102.8, 2000)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 60.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 1.5)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    result = engine._fifteen_minute_entry_confirmation(engine.convert_candles(candles), "LONG", 100.0, retest_time=27 * 900_000)
    assert result["ready"] is True
    assert result["trigger_type"] in {"BREAKOUT", "RECLAIM"}

    mirror = [
        _candle(i * 900_000, 100.0, 100.4, 99.6, 99.9) for i in range(30)
    ]
    mirror[-2] = _candle(28 * 900_000, 99.8, 100.1, 99.0, 99.5)
    mirror[-1] = _candle(29 * 900_000, 99.4, 99.6, 97.0, 97.2, 2000)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 40.0)
    mirror_result = engine._fifteen_minute_entry_confirmation(engine.convert_candles(mirror), "SHORT", 100.0, retest_time=27 * 900_000)
    assert mirror_result["ready"] is True
    assert mirror_result["trigger_type"] in {"BREAKDOWN", "RECLAIM"}


def test_engine_has_no_5m_confirmation_trigger():
    assert not hasattr(engine, "_five_minute_trigger")


def test_live_geometry_keeps_structural_stop_and_targets(monkeypatch):
    analysis = {
        "entry": 100.0,
        "stop_loss": 98.5,
        "tp": 106.5,
        "atr": 1.0,
    }
    ok, reason = MexcScanner._validate_live_geometry(analysis, 100.1, "LONG", max_drift_atr=0.20)
    assert ok, reason
    assert analysis["stop_loss"] == 98.5
    assert analysis["tp"] == 106.5


def test_simulator_intraday_expiry_and_costs():
    signal = {
        "symbol": "TEST_USDT", "setup": "LONG", "entry": 100.0,
        "stop_loss": 98.0, "tp": 106.0,
        "regime": "BULLISH",
    }
    future = [_candle(300_000 + i * 300_000, 100.0, 101.0, 99.0, 100.5) for i in range(4)]
    trade = simulate_trade(signal, future, signal_close_time_ms=300_000, fee_rate=0.0004, slippage_bps=2.0, max_holding_minutes=10)
    assert trade is not None
    assert trade.outcome == "EXPIRED"
    assert trade.expired is True
    assert trade.hold_minutes is not None and trade.hold_minutes <= 10.0

    winning = simulate_trade(
        signal,
        [_candle(600_000, 100.0, 107.0, 100.0, 106.0)],
        signal_close_time_ms=300_000,
        fee_rate=0.0004,
        slippage_bps=2.0,
        max_holding_minutes=360,
    )
    assert winning is not None
    assert winning.outcome == "TP"
    assert winning.fees_r > 0
    assert winning.slippage_r > 0
    assert winning.r_multiple < winning.planned_rr


def test_report_has_direction_regime_drawdown_and_expectancy_metrics():
    long_signal = {"symbol": "A_USDT", "setup": "LONG", "entry": 100, "stop_loss": 95, "tp": 110, "regime": "BULLISH"}
    short_signal = {"symbol": "B_USDT", "setup": "SHORT", "entry": 100, "stop_loss": 105, "tp": 90, "regime": "BEARISH"}
    t1 = simulate_trade(long_signal, [_candle(600_000, 100, 111, 100, 110)], signal_close_time_ms=300_000, fee_rate=0.0, slippage_bps=0.0)
    t2 = simulate_trade(short_signal, [_candle(900_000, 100, 105, 89, 90)], signal_close_time_ms=600_000, fee_rate=0.0, slippage_bps=0.0)
    summary = summarize(days=7, coins_selected=2, coins_tested=2, data_errors=0, trades=[t for t in (t1, t2) if t is not None])
    assert summary.expectancy_r == pytest.approx(0.5)
    assert summary.max_drawdown_r >= 0
    assert set(summary.direction_stats) == {"LONG", "SHORT"}
    assert set(summary.regime_stats) == {"BULLISH", "BEARISH"}


def test_weighted_supporting_factors_can_pass_with_five_of_eight_families():
    family_result = {"families": {
        "momentum": {"status": "PASS"},
        "relative_volume": {"status": "PASS"},
        "volatility_regime": {"status": "PASS"},
        "liquidity_quality": {"status": "ABSTAIN"},
        "funding_crowding": {"status": "ABSTAIN"},
        "flow_pressure": {"status": "PASS"},
        "htf_target_path": {"status": "PASS"},
        "vwap_location": {"status": "FAIL"},
    }, "passed": 5}
    score, groups, families = engine._build_score(
        direction_ok=True, structure_ok=True, setup_ok=True,
        momentum_ok=True, volume_ok=True, location_ok=True,
        futures_ok=False, volatility_ok=True, trigger_quality=0.8,
        rvol=1.2, bos_quality=0.8, retest_quality=0.8,
        momentum_quality=0.8, volume_quality=0.8, family_result=family_result,
    )
    assert score >= 78
    assert families == 5

def test_target_path_requires_higher_timeframe_target(monkeypatch):
    monkeypatch.setattr(engine, "_collect_structural_levels", lambda *args, **kwargs: [
        {"price": 103.0, "timeframe": "15M", "index": 1, "kind": "RESISTANCE"},
        {"price": 106.0, "timeframe": "15M", "index": 2, "kind": "RESISTANCE"},
    ])
    result = engine._target_path([("15M", [])], "LONG", 100.0, 97.0, 2.0)
    assert result["ok"] is False
    assert "1H/4H/1D" in result["reason"]

def test_intraday_structure_window_and_target_distance_are_widened():
    assert engine.MAX_SETUP_AGE_15M == 32
    assert engine.MIN_TP_ATR == pytest.approx(2.50)
    assert engine.MIN_SL_ATR == pytest.approx(1.00)
    assert engine.MAX_SL_ATR == pytest.approx(3.50)


def test_5m_confirmation_is_not_an_engine_decision_gate():
    assert not hasattr(engine, "ENABLE_5M_REFINEMENT")
    source = open(engine.__file__, encoding="utf-8").read()
    assert "_five_minute_trigger" not in source


def test_supporting_family_count_is_five_of_eight_with_diversity():
    family_result = {"families": {
        "momentum": {"status": "PASS"},
        "relative_volume": {"status": "PASS"},
        "volatility_regime": {"status": "PASS"},
        "liquidity_quality": {"status": "FAIL"},
        "funding_crowding": {"status": "ABSTAIN"},
        "flow_pressure": {"status": "PASS"},
        "htf_target_path": {"status": "PASS"},
        "vwap_location": {"status": "FAIL"},
    }, "passed": 5}
    score, _groups, families = engine._build_score(
        direction_ok=True, structure_ok=True, setup_ok=True,
        momentum_ok=True, volume_ok=True, location_ok=True,
        futures_ok=False, volatility_ok=True, trigger_quality=0.8,
        rvol=1.2, bos_quality=0.8, retest_quality=0.8,
        momentum_quality=0.8, volume_quality=0.8, family_result=family_result,
    )
    assert score >= 78
    assert families == 5

def test_structural_stop_floor_is_symmetric():
    long = calculate_trade_levels({
        "setup": "LONG", "price": 100.0, "atr": 1.0,
        "retest": {"low": 99.9, "high": 100.5},
        "protected_low": 99.9,
        "target_frames": [],
    })
    short = calculate_trade_levels({
        "setup": "SHORT", "price": 100.0, "atr": 1.0,
        "retest": {"low": 99.5, "high": 100.1},
        "protected_high": 100.1,
        "target_frames": [],
    })
    assert long["sl_atr"] == pytest.approx(engine.MIN_SL_ATR)
    assert short["sl_atr"] == pytest.approx(engine.MIN_SL_ATR)
