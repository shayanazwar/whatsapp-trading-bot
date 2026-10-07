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


def test_direction_alignment_uses_1d_and_4h_hard_gates_only():
    daily_bull = {"bull": True, "bear": False}
    neutral_12h = {"bull": False, "bear": False, "bull_votes": 1, "bear_votes": 4}
    assert engine._direction_aligned("LONG", daily_bull, neutral_12h, "RANGE") is True
    assert engine._direction_aligned("SHORT", daily_bull, neutral_12h, "RANGE") is False
    daily_neutral = {"bull": False, "bear": False}
    assert engine._direction_aligned("LONG", daily_neutral, neutral_12h, "RANGE") is True
    assert engine._direction_aligned("SHORT", daily_neutral, neutral_12h, "RANGE") is True


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
        engine.convert_candles(rows), "LONG", {"level": 100.0, "zone_floor": 99.75, "zone_ceiling": 100.50, "atr": 1.0, "time": retest_open}
    )
    assert result["ready"] is True
    assert result["trigger_type"] == "RETEST_RECLAIM"
    assert 0 <= result["bars_after_retest"] <= engine.MAX_RETEST_1H_BARS


def test_1h_trigger_does_not_use_bars_inside_departure_4h_candle(monkeypatch):
    base = 1_700_000_000_000
    rows = [candle(base + i * 3_600_000, 100, 100.4, 99.6, 100.1) for i in range(60)]
    rows[48] = candle(base + 48 * 3_600_000, 99.8, 100.1, 99.7, 99.9, 2000)
    rows[49] = candle(base + 49 * 3_600_000, 99.9, 100.8, 99.85, 100.5, 2000)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 55.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 1.20)
    result = engine._one_hour_trigger_confirmation(
        engine.convert_candles(rows), "LONG", {"level": 100.0, "zone_floor": 99.75, "zone_ceiling": 100.50, "atr": 1.0, "time": base + 44 * 3_600_000}
    )
    assert result["ready"] is False
    assert "retest found" in result["reason"].lower()

def test_1h_trigger_rejects_weak_execution_bar(monkeypatch):
    base = 1_700_000_000_000
    rows = [candle(base + i * 3_600_000, 100, 100.4, 99.6, 100.1) for i in range(50)]
    rows[-1] = candle(base + 49 * 3_600_000, 100.0, 100.6, 99.9, 100.2, 500)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 55.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 0.40)
    result = engine._one_hour_trigger_confirmation(
        engine.convert_candles(rows), "LONG", {"level": 100.0, "zone_floor": 99.75, "zone_ceiling": 100.50, "atr": 1.0, "time": base + 44 * 3_600_000}
    )
    assert result["ready"] is True
    assert result["confirmation_rvol_ok"] is False


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
    assert long["stop_source"].startswith("1H retest extreme")
    assert short["stop_source"].startswith("1H retest extreme")
    assert abs(100 - long["stop_loss"]) == pytest.approx(abs(short["stop_loss"] - 100))
    assert long["sl_atr"] >= engine.MIN_SL_ATR
    assert short["sl_atr"] >= engine.MIN_SL_ATR


def test_target_path_prefers_nearest_fresh_major_htf_target_and_keeps_4h_as_friction(monkeypatch):
    monkeypatch.setattr(engine, "_collect_structural_levels", lambda *args, **kwargs: [
        {"price": 102.75, "timeframe": "4H", "index": 1, "kind": "RESISTANCE"},
        {"price": 104.5, "timeframe": "12H", "index": 2, "kind": "RESISTANCE"},
        {"price": 108.0, "timeframe": "1D", "index": 3, "kind": "RESISTANCE"},
    ])
    levels = calculate_trade_levels({
        "setup": "LONG", "price": 100.0, "atr_1h": 1.0, "atr_4h": 1.0,
        "retest": {"low": 99.5}, "protected_low": 99.5,
        "target_frames": [("1D", []), ("12H", []), ("4H", [])],
    })
    assert levels["target_path_structural"] is True
    assert levels["target_timeframe"] == "12H"
    assert levels["tp"] == pytest.approx(104.35)
    assert levels["rr"] >= engine.MIN_RR


def test_target_path_requires_a_real_major_higher_timeframe_target(monkeypatch):
    monkeypatch.setattr(engine, "_collect_structural_levels", lambda *args, **kwargs: [
        {"price": 101.0, "timeframe": "1H", "index": 1, "kind": "RESISTANCE"},
        {"price": 101.5, "timeframe": "4H", "index": 2, "kind": "RESISTANCE"},
    ])
    result = engine._target_path([("1D", []), ("12H", []), ("4H", []), ("1H", [])], "LONG", 100.0, 99.0, 1.0)
    assert result["ok"] is False
    assert result["structural"] is False
    assert "1D/12H" in result["reason"]


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
    assert set(groups) == {"breakout_quality", "retest_quality", "12h_context", "entry_efficiency", "momentum", "volume", "volatility"}
    # Mandatory gates are intentionally absent from the score API.


def test_confirmation_families_are_supporting_evidence_not_a_hard_gate():
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
    assert out["available"] == 5
    assert out["passed"] >= 2
    assert out["diversity_ok"] is True
    assert out["hard_gate"] is False


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


def test_simulator_market_entry_uses_next_1h_open():
    signal = {"symbol": "TEST_USDT", "setup": "LONG", "entry": 100.0, "stop_loss": 95.0, "tp": 115.0, "entry_mode": "MARKET"}
    future = [candle(3_600_000, 105.0, 116.0, 104.0, 115.0)]
    trade = simulate_trade(signal, future, signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0)
    assert trade is not None
    assert trade.entry_execution == pytest.approx(105.0)
    assert trade.entry_filled_time_ms == 3_600_000
    assert trade.outcome == "TP"


def test_report_tracks_expectancy_and_drawdown():
    a = simulate_trade({"symbol": "A", "setup": "LONG", "entry": 100, "stop_loss": 95, "tp": 110}, [candle(3_600_000, 100, 111, 100, 110)], signal_close_time_ms=0, fee_rate=0, slippage_bps=0)
    b = simulate_trade({"symbol": "B", "setup": "SHORT", "entry": 100, "stop_loss": 105, "tp": 90}, [candle(3_600_000, 100, 106, 94, 100)], signal_close_time_ms=0, fee_rate=0, slippage_bps=0)
    summary = summarize(days=1, period_start_ms=0, period_end_ms=86_400_000, coins_selected=2, coins_tested=2, data_errors=0, execution_errors=0, rejected_setups=0, trades=[x for x in (a, b) if x])
    assert summary.expectancy_r is not None
    assert summary.max_drawdown_r >= 0


def test_retest_requires_meaningful_departure():
    base = 1_700_000_000_000
    rows = [candle(base + i * 14_400_000, 100.0, 100.4, 99.7, 100.2) for i in range(30)]
    # No 0.75 ATR displacement away from the BOS level before the pullback.
    bos = {"index": 20, "time": rows[20][0], "level": 100.0, "atr": 1.0, "strength": 0.8}
    rows[21] = candle(base + 21 * 14_400_000, 100.2, 100.6, 99.8, 100.0)
    rows[22] = candle(base + 22 * 14_400_000, 100.0, 100.3, 99.7, 99.9)
    out = engine._pullback_retest(engine.convert_candles(rows), "LONG", bos, max_bars=6)
    assert out["valid"] is False


def test_direction_gate_requires_4h_structure_but_allows_12h_non_opposition():
    daily = {"bear": False, "bull": False}
    assert engine._direction_aligned("LONG", daily, {}, "LH/LL") is False
    assert engine._direction_aligned("SHORT", daily, {}, "HH/HL") is False
    assert engine._direction_aligned("LONG", {"bear": True, "bull": False}, {}, "HH/HL") is False
    assert engine._direction_aligned("SHORT", {"bear": False, "bull": True}, {}, "LH/LL") is False


def test_1h_trigger_rejects_stale_confirmation_bar(monkeypatch):
    base = 1_700_000_000_000
    rows = [candle(base + i * 3_600_000, 100.0, 100.4, 99.6, 100.1) for i in range(55)]
    rows[48] = candle(base + 48 * 3_600_000, 99.8, 100.1, 99.7, 99.9, 2000)
    rows[49] = candle(base + 49 * 3_600_000, 100.0, 100.8, 99.9, 100.6, 2000)
    for i in range(50, 55):
        rows[i] = candle(base + i * 3_600_000, 101.8, 102.2, 101.6, 102.0)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 55.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 1.20)
    result = engine._one_hour_trigger_confirmation(
        engine.convert_candles(rows),
        "LONG",
        {"level": 100.0, "zone_floor": 99.65, "zone_ceiling": 100.35, "atr": 1.0, "time": base + 44 * 3_600_000},
    )
    assert result["ready"] is False
    assert result["retest_found"] is True
    assert "confirmation" in result["reason"].lower()


def test_1h_trigger_routes_extended_confirmation_to_limit(monkeypatch):
    base = 1_700_000_000_000
    rows = [candle(base + i * 3_600_000, 100.0, 100.4, 99.6, 100.1) for i in range(52)]
    rows[48] = candle(base + 48 * 3_600_000, 99.8, 100.1, 99.7, 99.9, 2000)
    rows[49] = candle(base + 49 * 3_600_000, 100.0, 101.4, 99.9, 101.2, 2000)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 55.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 1.20)
    result = engine._one_hour_trigger_confirmation(
        engine.convert_candles(rows),
        "LONG",
        {"level": 100.0, "zone_floor": 99.75, "zone_ceiling": 100.50, "atr": 1.0, "time": base + 44 * 3_600_000},
    )
    assert result["ready"] is True
    assert result["entry_mode"] == "MARKET"
    assert result["entry_price"] == pytest.approx(result["entry_reference_close"])
    assert result["entry_distance_reference_atr"] == "1H"
