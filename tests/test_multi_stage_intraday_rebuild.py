from __future__ import annotations

import math

import pytest

import app.analysis.engine as engine
from app.automation.scanner import MexcScanner
from app.backtest.report import format_report, summarize
from app.backtest.simulator import simulate_trade


M5 = 300_000


def candle(ts: int, o: float, h: float, l: float, c: float, v: float = 100.0):
    return [ts, o, h, l, c, v]


def long_signal(**overrides):
    signal = {
        "symbol": "TEST_USDT",
        "setup": "LONG",
        "entry": 100.0,
        "stop_loss": 95.0,
        "tp1": 106.0,
        "tp2": 110.0,
        "position_size": 1.0,
        "contract_size": 1.0,
    }
    signal.update(overrides)
    return signal


def short_signal(**overrides):
    signal = {
        "symbol": "TEST_USDT",
        "setup": "SHORT",
        "entry": 100.0,
        "stop_loss": 105.0,
        "tp1": 94.0,
        "tp2": 90.0,
        "position_size": 1.0,
        "contract_size": 1.0,
    }
    signal.update(overrides)
    return signal


def test_long_tp1_then_breakeven_is_50_50_and_never_reuses_original_sl():
    trade = simulate_trade(
        long_signal(),
        [
            candle(M5, 100.0, 107.0, 99.0, 106.5),
            candle(2 * M5, 106.5, 107.0, 99.0, 100.0),
        ],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert trade is not None
    assert trade.outcome == "BE"
    assert trade.tp1_hit is True
    assert trade.tp2_hit is False
    assert trade.sl_hit is False
    assert trade.breakeven_hit is True
    assert trade.tp1_close_size == pytest.approx(0.5)
    assert trade.final_close_size == pytest.approx(0.5)
    assert trade.remaining_position_size == pytest.approx(0.0)
    assert trade.gross_pnl == pytest.approx(3.0)
    assert trade.r_multiple == pytest.approx(0.6)
    assert trade.breakeven_stop == pytest.approx(100.0)
    assert trade.breakeven_execution == pytest.approx(100.0)


def test_short_tp1_then_tp2_realized_r_is_weighted_by_half_position():
    trade = simulate_trade(
        short_signal(),
        [candle(M5, 100.0, 100.0, 89.0, 90.0)],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert trade is not None
    assert trade.outcome == "TP2"
    assert trade.tp1_hit and trade.tp2_hit
    assert trade.gross_pnl == pytest.approx(8.0)
    assert trade.r_multiple == pytest.approx(1.6)
    assert trade.tp1_close_size == pytest.approx(0.5)
    assert trade.final_close_size == pytest.approx(0.5)
    assert trade.remaining_position_size == pytest.approx(0.0)


def test_same_bar_sl_first_is_pessimistic_and_does_not_partial_fill_tp1():
    trade = simulate_trade(
        long_signal(),
        [candle(M5, 100.0, 111.0, 94.0, 100.0)],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
        same_bar_rule="SL_FIRST",
    )
    assert trade is not None
    assert trade.outcome == "SL"
    assert trade.sl_hit is True
    assert trade.tp1_hit is False
    assert trade.tp2_hit is False
    assert trade.remaining_position_size == pytest.approx(0.0)
    assert trade.r_multiple == pytest.approx(-1.0)


def test_same_bar_tp_first_can_finish_both_profit_stages():
    trade = simulate_trade(
        long_signal(),
        [candle(M5, 100.0, 111.0, 94.0, 100.0)],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
        same_bar_rule="TP_FIRST",
    )
    assert trade is not None
    assert trade.outcome == "TP2"
    assert trade.tp1_hit and trade.tp2_hit
    assert trade.sl_hit is False
    assert trade.r_multiple == pytest.approx(1.6)
    assert trade.remaining_position_size == pytest.approx(0.0)


def test_post_tp1_same_bar_collision_obeys_rule():
    future = [
        candle(M5, 100.0, 107.0, 101.0, 106.0),
        candle(2 * M5, 106.0, 111.0, 99.0, 105.0),
    ]
    conservative = simulate_trade(
        long_signal(), future, signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0, same_bar_rule="SL_FIRST"
    )
    permissive = simulate_trade(
        long_signal(), future, signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0, same_bar_rule="TP_FIRST"
    )
    assert conservative is not None and permissive is not None
    assert conservative.outcome == "BE"
    assert conservative.breakeven_hit is True
    assert permissive.outcome == "TP2"
    assert permissive.tp2_hit is True


def test_position_step_rejects_unrepresentable_exact_half_without_exception():
    trade = simulate_trade(
        long_signal(position_size=3.0, position_step=1.0),
        [candle(M5, 100.0, 111.0, 100.0, 110.0)],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert trade is None


def test_position_step_accepts_exact_half_when_representable():
    trade = simulate_trade(
        long_signal(position_size=4.0, position_step=1.0),
        [candle(M5, 100.0, 111.0, 100.0, 110.0)],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert trade is not None
    assert trade.tp1_close_size == pytest.approx(2.0)
    assert trade.final_close_size == pytest.approx(2.0)
    assert trade.remaining_position_size == pytest.approx(0.0)


def test_costs_are_realized_across_entry_tp1_and_final_exit():
    trade = simulate_trade(
        long_signal(),
        [candle(M5, 100.0, 111.0, 100.0, 110.0)],
        signal_close_time_ms=M5,
        fee_rate=0.001,
        slippage_bps=10.0,
    )
    assert trade is not None
    assert trade.outcome == "TP2"
    assert trade.entry_fee > 0
    assert trade.exit_fees > 0
    assert trade.fees_r > 0
    assert trade.slippage_r > 0
    assert trade.r_multiple < trade.planned_rr
    assert math.isfinite(trade.realized_pnl)


def test_invalid_future_candles_do_not_raise_or_create_orphan_state():
    trade = simulate_trade(
        long_signal(),
        [None, {}, [1, 2, 3], [M5, 100.0, float("nan"), 99.0, 100.0, 100.0]],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert trade is None


def test_no_future_data_returns_none_instead_of_fabricating_an_exit():
    trade = simulate_trade(long_signal(), [], signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0)
    assert trade is None



def test_report_exposes_breakeven_stage():
    trade = simulate_trade(
        long_signal(),
        [candle(M5, 100.0, 107.0, 99.0, 106.0), candle(2 * M5, 106.0, 106.0, 99.0, 100.0)],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert trade is not None
    summary = summarize(days=1, coins_selected=1, coins_tested=1, data_errors=0, trades=[trade])
    rendered = format_report(summary)
    assert "TP1→BE: 1" in rendered
    assert "TOTAL R: +0.60R" in rendered


def test_sideways_four_hour_needs_four_of_four_one_hour_votes():
    sideways = {"bull": False, "bear": False}
    assert engine._direction_aligned("LONG", sideways, {"long": True, "long_votes": 4}) is True
    assert engine._direction_aligned("LONG", sideways, {"long": True, "long_votes": 3}) is False
    assert engine._direction_aligned("SHORT", sideways, {"short": True, "short_votes": 4}) is True
    assert engine._direction_aligned("SHORT", sideways, {"short": True, "short_votes": 3}) is False


def test_direction_never_trades_against_confirmed_four_hour_trend():
    assert engine._direction_aligned("LONG", {"bull": True, "bear": False}, {"long": True}) is True
    assert engine._direction_aligned("SHORT", {"bull": True, "bear": False}, {"short": True}) is False
    assert engine._direction_aligned("SHORT", {"bull": False, "bear": True}, {"short": True}) is True
    assert engine._direction_aligned("LONG", {"bull": False, "bear": True}, {"long": True}) is False


def test_5m_trigger_requires_bos_strong_candle_and_expanding_volume(monkeypatch):
    candles = [candle(i * M5, 100.0, 100.2, 99.8, 100.0, 100.0) for i in range(29)]
    candles.append(candle(29 * M5, 100.2, 102.2, 100.0, 102.0, 2_000.0))
    converted = engine.convert_candles(candles)
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 65.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 1.5)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    result = engine._five_minute_trigger(converted, "LONG", 100.5)
    assert result["ready"] is True
    assert result["trigger_type"] == "BOS_CONTINUATION"
    assert result["volume_expanding"] is True


def test_5m_trigger_rejects_missing_volume_expansion(monkeypatch):
    candles = [candle(i * M5, 100.0, 100.2, 99.8, 100.0, 100.0) for i in range(29)]
    candles.append(candle(29 * M5, 100.2, 102.2, 100.0, 102.0, 100.0))
    monkeypatch.setattr(engine, "_safe_rsi", lambda closes: 65.0)
    monkeypatch.setattr(engine, "_relative_volume", lambda candles: 1.5)
    monkeypatch.setattr(engine, "_safe_atr", lambda candles: 1.0)
    result = engine._five_minute_trigger(engine.convert_candles(candles), "LONG", 100.5)
    assert result["ready"] is False
    assert "expanding" in result["reason"]


def test_live_geometry_has_no_fixed_percent_stop_floor():
    analysis = {
        "entry": 100.0,
        "stop_loss": 99.38,
        "tp1": 102.0,
        "tp2": 104.0,
        "atr": 1.0,
    }
    ok, reason = MexcScanner._validate_live_geometry(analysis, 100.0, "LONG", max_drift_pct=0.01)
    assert ok, reason
    assert analysis["stop_distance_pct"] == pytest.approx(0.0062)
    assert analysis["tp1"] == 102.0
    assert analysis["tp2"] == 104.0
