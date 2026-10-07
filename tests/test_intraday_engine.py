from __future__ import annotations

import math

import pytest

import app.analysis.engine as engine
from app.automation.scanner import get_decision_close_ms, signal_fresh_enough
from app.backtest.report import summarize
from app.backtest.simulator import simulate_trade

HOUR = 3_600_000


def candle(ts: int, o: float, h: float, l: float, c: float, v: float = 1000.0):
    return [ts, o, h, l, c, v]


def _stub_candles(n: int = 60):
    return engine.convert_candles(
        [candle(1_700_000_000_000 + i * HOUR, 100, 101, 99, 100, 1000) for i in range(n)]
    )


def test_approved_signal_timeframes_are_exact_and_no_lower_timeframe_engine_gate_exists():
    assert engine.APPROVED_TIMEFRAMES == ("1D", "12H", "4H", "1H")
    source = open(engine.__file__, encoding="utf-8").read().upper()
    assert "5M" not in source
    assert "15M" not in source
    assert "30M" not in source
    assert "8H" not in source
    assert "1W" not in source


def test_live_decision_clock_is_exactly_the_completed_1h_boundary():
    now = 1_700_000_123_456
    decision = get_decision_close_ms(now)
    assert decision % HOUR == 0
    assert decision <= now < decision + HOUR
    assert signal_fresh_enough(decision, decision + 119_999, 120)
    assert not signal_fresh_enough(decision, decision + 120_001, 120)


def test_strict_long_sweep_requires_a_subsequent_reclaim_candle():
    base = 1_700_000_000_000
    rows = [candle(base + i * HOUR, 100, 101, 99, 100, 1000) for i in range(8)]
    # The prior three candles establish a 100 low. Sweep closes below it.
    rows[3] = candle(base + 3 * HOUR, 100, 101, 98, 99, 2000)
    result = engine._v11_liquidity_trigger(engine.convert_candles(rows[:4]), 3, "LONG")
    assert result["ready"] is False

    # The following candle reclaims the level and becomes the trigger.
    rows[4] = candle(base + 4 * HOUR, 98.8, 102, 98.5, 101, 2500)
    result = engine._v11_liquidity_trigger(engine.convert_candles(rows[:5]), 3, "LONG")
    assert result["ready"] is True
    assert result["sweep_idx"] == 3
    assert result["reclaim_idx"] == 4


def test_strict_short_sweep_and_reclaim_is_symmetric():
    base = 1_700_000_000_000
    rows = [candle(base + i * HOUR, 100, 101, 99, 100, 1000) for i in range(8)]
    rows[3] = candle(base + 3 * HOUR, 100, 103, 99, 102, 2000)
    result = engine._v11_liquidity_trigger(engine.convert_candles(rows[:4]), 3, "SHORT")
    assert result["ready"] is False
    rows[4] = candle(base + 4 * HOUR, 102.2, 103, 98, 99, 2500)
    result = engine._v11_liquidity_trigger(engine.convert_candles(rows[:5]), 3, "SHORT")
    assert result["ready"] is True
    assert result["sweep_idx"] == 3
    assert result["reclaim_idx"] == 4


def test_long_4h_impulse_requires_true_hh_hl_sequence(monkeypatch):
    candles = _stub_candles(60)
    highs = [(10, 110.0), (30, 130.0)]
    lows = [(5, 100.0), (20, 115.0)]
    monkeypatch.setattr(engine, "_swing_points", lambda *args, **kwargs: (highs, lows))
    monkeypatch.setattr(engine, "_atr_series", lambda *args, **kwargs: [1.0] * len(candles))
    out = engine._v11_find_impulses(candles, "LONG")
    assert out
    assert out[-1]["low"] == pytest.approx(115.0)
    assert out[-1]["high"] == pytest.approx(130.0)
    assert out[-1]["prior_low"] == pytest.approx(100.0)
    assert out[-1]["prior_high"] == pytest.approx(110.0)

    bad_lows = [(5, 100.0), (20, 95.0)]
    monkeypatch.setattr(engine, "_swing_points", lambda *args, **kwargs: (highs, bad_lows))
    assert engine._v11_find_impulses(candles, "LONG") == []


def test_short_4h_impulse_requires_true_lh_ll_sequence(monkeypatch):
    candles = _stub_candles(60)
    highs = [(5, 130.0), (20, 115.0)]
    lows = [(10, 110.0), (30, 95.0)]
    monkeypatch.setattr(engine, "_swing_points", lambda *args, **kwargs: (highs, lows))
    monkeypatch.setattr(engine, "_atr_series", lambda *args, **kwargs: [1.0] * len(candles))
    out = engine._v11_find_impulses(candles, "SHORT")
    assert out
    assert out[-1]["high"] == pytest.approx(115.0)
    assert out[-1]["low"] == pytest.approx(95.0)

    bad_highs = [(5, 130.0), (20, 135.0)]
    monkeypatch.setattr(engine, "_swing_points", lambda *args, **kwargs: (bad_highs, lows))
    assert engine._v11_find_impulses(candles, "SHORT") == []


def test_short_target_selects_nearest_structural_level(monkeypatch):
    c4 = _stub_candles(50)
    c12 = _stub_candles(50)
    c1d = _stub_candles(50)
    c1 = _stub_candles(10)

    def fake_levels(candles, timeframe, side, entry):
        if timeframe == "4H":
            return [
                {"price": 90.0, "timeframe": "4H", "index": 40, "time": 1_700_000_000_000 + 40 * HOUR, "kind": "SUPPORT"},
                {"price": 80.0, "timeframe": "4H", "index": 42, "time": 1_700_000_000_000 + 42 * HOUR, "kind": "SUPPORT"},
            ]
        return []

    monkeypatch.setattr(engine, "_fresh_structural_levels", fake_levels)
    impulse = {"side": "SHORT", "high": 110.0, "low": 90.0, "high_idx": 20, "low_idx": 40, "high_time": c4[20]["time"], "low_time": c4[40]["time"]}
    result = engine._v11_target(c4, c12, c1d, c1, 100.0, "SHORT", impulse, 5.0)
    assert result["ok"] is True
    assert result["price"] == pytest.approx(90.0)


def test_target_path_is_rejected_when_a_real_1h_structural_blocker_exists(monkeypatch):
    c4 = _stub_candles(50)
    c12 = _stub_candles(50)
    c1d = _stub_candles(50)
    c1 = _stub_candles(10)

    def fake_levels(candles, timeframe, side, entry):
        if timeframe == "4H":
            return [{"price": 130.0, "timeframe": "4H", "index": 40, "time": 1_700_000_000_000 + 40 * HOUR, "kind": "RESISTANCE"}]
        if timeframe == "1H":
            return [{"price": 120.0, "timeframe": "1H", "index": 6, "time": 1_700_000_000_000 + 6 * HOUR, "kind": "RESISTANCE"}]
        return []

    monkeypatch.setattr(engine, "_fresh_structural_levels", fake_levels)
    # Mark the impulse target as consumed so the nearest structural target is 130.
    for i in range(21, len(c4)):
        c4[i]["high"] = 150.0
    impulse = {"side": "LONG", "high": 150.0, "low": 105.0, "high_idx": 20, "low_idx": 10, "high_time": c4[20]["time"], "low_time": c4[10]["time"]}
    result = engine._v11_target(c4, c12, c1d, c1, 110.0, "LONG", impulse, 5.0)
    assert result["structural"] is True
    assert result["path_clear"] is False
    assert result["blocking_levels"]
    assert result["blocking_levels"][0]["price"] == pytest.approx(120.0)


def test_market_executor_payload_uses_mexc_market_type_for_both_sides():
    from app.automation.executor import build_market_order_payload
    from app.automation.universe import ContractMeta
    from app.automation.signal_validator import validate_signal

    def data(side):
        return {
            "symbol": "TEST_USDT", "setup": side, "candle_time": int(__import__("time").time() * 1000),
            "direction_ok": True, "structure_ok": True, "setup_ok": True, "confirmation_ok": True,
            "location_ok": True, "target_path_structural": True, "target_path_clear": True,
            "structure_quality_ok": True, "shock_veto_ok": True, "technical_candidate": True,
            "trade_geometry_ok": True, "risk_ok": True, "volatility_ok": True,
            "primary_entry_timeframe": "1H", "signal_candle_timeframe": "1H", "score_hard_gate": False,
            "entry_mode": "MARKET", "entry": 100.0, "stop_loss": 95.0 if side == "LONG" else 105.0,
            "tp": 110.0 if side == "LONG" else 90.0, "rr": 1.9, "atr_4h": 5.0, "tp_distance_atr": 2.0,
            "mexc_spread_pct": 0.01, "max_allowed_spread_pct": 0.50,
            "max_signal_age_seconds": 120, "confirmation_family_diversity_ok": True,
        }

    meta = ContractMeta("TEST_USDT", "USDT", "USDT", 1.0, 1.0, 1, 1, 10000, 1, 0, 0, True, False, 1, False)
    for side, expected in (("LONG", 1), ("SHORT", 3)):
        signal, reasons = validate_signal(data(side), min_confluence=0, min_rr=1.6)
        assert signal is not None, reasons
        payload = build_market_order_payload(signal, meta, risk_amount_usdt=10, leverage=3, open_type=1)
        assert payload["type"] == 5
        assert payload["side"] == expected


def test_backtest_market_fill_reports_signal_rr_and_actual_fill_rr_separately():
    signal = {"symbol": "TEST_USDT", "setup": "LONG", "entry": 100.0, "stop_loss": 95.0, "tp": 110.0, "entry_mode": "MARKET"}
    future = [candle(HOUR, 105.0, 111.0, 104.0, 110.0)]
    trade = simulate_trade(signal, future, signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0)
    assert trade is not None
    assert trade.signal_entry == pytest.approx(100.0)
    assert trade.entry_execution == pytest.approx(105.0)
    assert trade.actual_fill_rr == pytest.approx(0.5)
    assert trade.planned_rr == pytest.approx(0.5)

    summary = summarize(days=1, period_start_ms=0, period_end_ms=86_400_000, coins_selected=1, coins_tested=1, data_errors=0, execution_errors=0, rejected_setups=0, trades=[trade])
    assert summary.avg_signal_rr == pytest.approx(2.0)
    assert summary.avg_actual_fill_rr == pytest.approx(0.5)
    assert summary.avg_planned_rr == pytest.approx(0.5)


def test_settings_shared_cost_model_matches_backtest_execution_plus_funding():
    from app.config import Settings

    settings = Settings(
        estimated_round_trip_cost_pct=0.0012,
        estimated_funding_cost_pct=0.0002,
        backtest_fee_rate=0.0006,
        backtest_slippage_bps=2.0,
    )
    assert settings.backtest_execution_cost_pct == pytest.approx(0.0016)
    assert settings.effective_round_trip_cost_pct == pytest.approx(0.0018)


def test_shared_cost_model_does_not_change_with_live_funding_quote():
    from app.automation.signal_validator import validate_signal
    import time
    base = {
        "symbol": "TEST_USDT", "setup": "LONG", "candle_time": int(time.time() * 1000),
        "direction_ok": True, "structure_ok": True, "setup_ok": True, "confirmation_ok": True,
        "location_ok": True, "target_path_structural": True, "target_path_clear": True,
        "structure_quality_ok": True, "shock_veto_ok": True, "technical_candidate": True,
        "trade_geometry_ok": True, "risk_ok": True, "volatility_ok": True,
        "primary_entry_timeframe": "1H", "signal_candle_timeframe": "1H", "entry_mode": "MARKET",
        "entry": 100.0, "stop_loss": 95.0, "tp": 109.0, "rr": 1.8, "atr_4h": 5.0,
        "tp_distance_atr": 1.8, "confirmation_family_diversity_ok": True,
        "mexc_spread_pct": 0.01, "max_allowed_spread_pct": 0.50,
        "estimated_round_trip_cost_pct": 0.0012, "estimated_funding_cost_pct": 0.0002,
        "max_signal_age_seconds": 120,
    }
    a = dict(base, mexc_funding_rate=0.00001)
    b = dict(base, mexc_funding_rate=0.01)
    sa, ra = validate_signal(a, min_confluence=0, min_rr=1.6)
    sb, rb = validate_signal(b, min_confluence=0, min_rr=1.6)
    assert sa is not None, ra
    assert sb is not None, rb
    assert sa.analysis["estimated_round_trip_cost_pct"] == pytest.approx(0.0018)
    assert sb.analysis["estimated_round_trip_cost_pct"] == pytest.approx(0.0018)


def test_v11_engine_has_no_legacy_v10_strategy_symbols():
    source = open(engine.__file__, encoding="utf-8").read()
    for legacy in (
        "_select_latest_bos_with_departure",
        "_pullback_retest",
        "_one_hour_trigger_confirmation",
        "_target_path",
        "calculate_trade_levels",
        "evaluate_confirmation_families",
    ):
        assert legacy not in source


def test_only_one_copy_of_critical_v11_helpers_exists():
    source = open(engine.__file__, encoding="utf-8").read().splitlines()
    for name in ("build_btc_context", "btc_filter_ok", "calculate_confluence", "_diagnostic_failures"):
        assert sum(line.startswith(f"def {name}(") for line in source) == 1


def test_risk_manager_default_cost_buffer_matches_v11_shared_cost():
    from app.automation.risk_manager import DEFAULT_COST_BUFFER_PCT
    assert DEFAULT_COST_BUFFER_PCT == pytest.approx(0.0018)


def test_supported_backtest_windows_extend_through_365d():
    from app.backtest.runner import SUPPORTED_BACKTEST_DAYS
    assert {1, 7, 30, 60, 90, 180, 365}.issubset(SUPPORTED_BACKTEST_DAYS)
