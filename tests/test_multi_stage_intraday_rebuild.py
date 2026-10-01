from __future__ import annotations

import pytest

from app.analysis.engine import evaluate_confirmation_families
from app.automation.scanner import MexcScanner
from app.backtest.report import format_report, summarize
from app.backtest.simulator import simulate_trade

M5 = 300_000

def candle(ts, o, h, l, c, v=1000.0):
    return [ts, o, h, l, c, v]

def signal(side="LONG", **overrides):
    base = {"symbol":"TEST_USDT","setup":side,"entry":100.0,"stop_loss":95.0 if side == "LONG" else 105.0,"tp":110.0 if side == "LONG" else 90.0,"position_size":1.0,"contract_size":1.0}
    base.update(overrides)
    return base

def test_single_tp_long_wins_without_partial_or_breakeven():
    trade = simulate_trade(signal(), [candle(M5,100,111,100,110)], signal_close_time_ms=M5, fee_rate=0, slippage_bps=0)
    assert trade is not None and trade.outcome == "TP"
    assert trade.tp1_hit and trade.tp2_hit
    assert trade.breakeven_hit is False
    assert trade.tp1_close_size == pytest.approx(0)
    assert trade.final_close_size == pytest.approx(1)
    assert trade.remaining_position_size == pytest.approx(0)
    assert trade.r_multiple == pytest.approx(2.0)

def test_single_tp_long_stops_before_target():
    trade = simulate_trade(signal(), [candle(M5,100,101,94,99)], signal_close_time_ms=M5, fee_rate=0, slippage_bps=0)
    assert trade is not None and trade.outcome == "SL" and trade.sl_hit
    assert trade.r_multiple == pytest.approx(-1.0)

def test_same_bar_rule_is_deterministic():
    future=[candle(M5,100,111,94,100)]
    conservative=simulate_trade(signal(), future, signal_close_time_ms=M5, fee_rate=0, slippage_bps=0, same_bar_rule="SL_FIRST")
    permissive=simulate_trade(signal(), future, signal_close_time_ms=M5, fee_rate=0, slippage_bps=0, same_bar_rule="TP_FIRST")
    assert conservative is not None and conservative.outcome == "SL"
    assert permissive is not None and permissive.outcome == "TP"

def test_single_tp_expiry_realizes_previous_close():
    future=[candle(M5+i*M5,100,101,99,100.5) for i in range(4)]
    trade=simulate_trade(signal(), future, signal_close_time_ms=M5, fee_rate=0, slippage_bps=0, max_holding_minutes=10)
    assert trade is not None and trade.outcome == "EXPIRED" and trade.expired
    assert trade.hold_minutes is not None and trade.hold_minutes <= 10

def test_single_tp_costs_reduce_realized_r():
    trade=simulate_trade(signal(), [candle(M5,100,111,100,110)], signal_close_time_ms=M5, fee_rate=0.001, slippage_bps=10)
    assert trade is not None and trade.outcome == "TP"
    assert trade.fees_r > 0 and trade.slippage_r > 0
    assert trade.r_multiple < trade.planned_rr

def test_live_geometry_requires_real_single_tp():
    analysis={"entry":100.0,"stop_loss":98.0,"tp":106.0,"atr":1.0}
    ok, reason=MexcScanner._validate_live_geometry(analysis,100.0,"LONG",max_drift_pct=0.01)
    assert ok, reason
    assert analysis["tp"] == pytest.approx(106.0)
    assert analysis["tp_distance_atr"] == pytest.approx(6.0)

def test_report_uses_single_tp_metrics():
    trade=simulate_trade(signal(), [candle(M5,100,111,100,110)], signal_close_time_ms=M5, fee_rate=0, slippage_bps=0)
    summary=summarize(days=7,coins_selected=1,coins_tested=1,data_errors=0,trades=[trade])
    text=format_report(summary)
    assert "TP HIT: 1" in text
    assert "AVG TP DIST" in text
    assert "Win rate = single TP before SL" in text
    assert "TP1→BE" not in text

def test_confirmation_families_require_five_and_diversity():
    data={"setup":"LONG","momentum_quality":0.8,"rvol_15m":1.5,"volatility_ok":True,"entry":100,"tp":105,"atr":1,"target_path_ok":True,"target_path_structural":True,"flow_proxy_ratio":0.2,"rolling_vwap_12h":99}
    out=evaluate_confirmation_families(data)
    assert out["passed"] >= 5
    assert out["available"] >= 6
    assert out["diversity_ok"] is True
