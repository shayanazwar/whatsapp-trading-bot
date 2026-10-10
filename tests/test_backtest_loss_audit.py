from __future__ import annotations

import logging
from dataclasses import replace

from app.backtest.runner import _log_forensic_trade_audit
from app.backtest.simulator import SimulatedTrade
from app.backtest.report import format_report, summarize


def _trade(outcome: str) -> SimulatedTrade:
    return SimulatedTrade(
        symbol="ABC_USDT", side="LONG", signal_time_ms=1_000,
        entry=100.0, stop_loss=95.0, tp1=110.0, tp2=110.0,
        planned_rr=2.0, signal_rr=2.0, actual_fill_rr=1.9,
        tp1_hit=False, tp2_hit=False, sl_hit=(outcome == "SL"),
        outcome=outcome, r_multiple=-1.05 if outcome == "SL" else -0.12,
        exit_time_ms=7_201_000, entry_filled_time_ms=3_601_000,
        entry_execution=100.1, exit_execution=95.0 if outcome == "SL" else 99.5,
        expired=(outcome == "EXPIRED"), mae_r=1.0, mfe_r=0.7,
        regime="BULLISH", entry_mode="MARKET", quality={"score": 84.0},
    )


def test_loss_audit_logs_sl_and_expired_details(caplog):
    with caplog.at_level(logging.INFO, logger="app.backtest.runner"):
        _log_forensic_trade_audit([_trade("SL"), _trade("EXPIRED"), _trade("TP")])
    output = "\n".join(record.getMessage() for record in caplog.records)
    assert "BACKTEST LOSS AUDIT START | sl=1 expired=1 total=2" in output
    assert "outcome=SL reason=STOP_LOSS_TOUCHED" in output
    assert "outcome=EXPIRED reason=MAX_HOLDING_LIMIT" in output
    assert "mae_r=1.000 mfe_r=0.700" in output
    assert "quality={\"score\":84.0}" in output
    assert "BACKTEST LOSS AUDIT END | audited=2" in output


def test_portfolio_drawdown_and_losing_streak_follow_realized_exit_order():
    # Signal order is SL, TP, SL; actual exit order is TP, SL, SL.
    first_signal_last_exit = replace(
        _trade("SL"), symbol="A_USDT", signal_time_ms=10, exit_time_ms=30, r_multiple=-1.2,
    )
    second_signal_first_exit = replace(
        _trade("TP"), symbol="B_USDT", signal_time_ms=20, exit_time_ms=25, r_multiple=2.0,
    )
    third_signal_last_exit = replace(
        _trade("SL"), symbol="C_USDT", signal_time_ms=30, exit_time_ms=40, r_multiple=-1.0,
    )

    summary = summarize(
        days=7, period_start_ms=0, period_end_ms=100,
        coins_selected=3, coins_tested=3, data_errors=0,
        execution_errors=0, rejected_setups=0,
        trades=[first_signal_last_exit, second_signal_first_exit, third_signal_last_exit],
    )
    assert summary.max_drawdown_r == 2.2
    assert summary.max_losing_streak == 2
    assert format_report(summary).count("Total R:") == 1
