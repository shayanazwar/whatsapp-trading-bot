from __future__ import annotations

import asyncio
import pathlib

import pytest

from app.backtest.report import format_report, summarize
from app.backtest.runner import BacktestAlreadyRunning, BacktestRunner, MAX_BACKTEST_SYMBOLS
from app.backtest.simulator import simulate_trade
from app.automation.mexc_client import MexcClient
from app.config import Settings


def candle(ts, o, h, l, c, v=1000.0):
    return [ts, o, h, l, c, v]


def test_backtest_universe_is_capped_at_200_symbols():
    assert MAX_BACKTEST_SYMBOLS == 200


def test_backtest_runner_supports_documented_validation_windows():
    class EmptyUniverse:
        async def refresh(self): return []
    runner = BacktestRunner(client=None, universe=EmptyUniverse(), settings=Settings(), max_concurrency=1)
    for days in (1, 7, 30, 60, 90, 180, 365):
        summary = asyncio.run(runner.run(days))
        assert summary.days == days
        assert summary.signals == 0
    with pytest.raises(ValueError):
        asyncio.run(runner.run(15))


def test_backtest_rejects_concurrent_job():
    class EmptyUniverse:
        async def refresh(self): return []
    runner = BacktestRunner(client=None, universe=EmptyUniverse(), settings=Settings(), max_concurrency=1)
    runner._running = True
    try:
        with pytest.raises(BacktestAlreadyRunning):
            asyncio.run(runner.run(1))
    finally:
        runner._running = False


def test_historical_kline_range_parser_uses_only_strategy_intervals():
    client = object.__new__(MexcClient)
    calls = {}
    async def fake_request(method, path, *, params=None, json_body=None, private=False):
        calls.update(params or {})
        return {
            "time": [1000, 1001], "open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, 2.5], "vol": [10, 20]
        }
    client._request = fake_request
    rows = asyncio.run(client.get_klines_range("ABC_USDT", "Min60", 1_000_000, 2_000_000, limit=100))
    assert calls["interval"] == "Min60"
    assert rows[0][0] == 1_000_000
    with pytest.raises(ValueError):
        asyncio.run(client.get_klines_range("ABC_USDT", "Min15", 1000, 2000, limit=10))


def test_backtest_engine_source_has_no_lower_timeframe_signal_dependency():
    source = pathlib.Path("app/backtest/runner.py").read_text(encoding="utf-8").upper()
    assert "5M" not in source
    assert "15M" not in source


def test_backtest_simulator_same_bar_rule_is_conservative_by_default():
    signal = {"symbol": "ABC_USDT", "setup": "LONG", "entry": 100, "stop_loss": 95, "tp": 110}
    trade = simulate_trade(signal, [candle(3_600_000, 100, 111, 94, 100)], signal_close_time_ms=0, fee_rate=0, slippage_bps=0)
    assert trade is not None
    assert trade.outcome == "SL"


def test_zero_trade_report_is_rendered_with_bias_diagnostic():
    summary = summarize(
        days=1,
        period_start_ms=0,
        period_end_ms=86_400_000,
        coins_selected=10,
        coins_tested=10,
        data_errors=0,
        execution_errors=0,
        rejected_setups=10,
        trades=[],
        diagnostics={"NO_VALID_SETUP": 10, "CURRENT_UNIVERSE_SNAPSHOT_BIAS": 1},
    )
    text = format_report(summary)
    assert "Signals: 0" in text
    assert "CURRENT_UNIVERSE_SNAPSHOT_BIAS" in text


def test_simulator_reports_signal_rr_and_actual_fill_rr_separately():
    signal = {
        "symbol": "ABC_USDT",
        "setup": "LONG",
        "entry": 100.0,
        "entry_mode": "MARKET",
        "stop_loss": 95.0,
        "tp": 110.0,
    }
    # Decision is made at t=0; the next 1H bar opens at 102, so actual fill
    # geometry differs from the signal-close geometry.
    future = [candle(3_600_000, 102.0, 111.0, 100.0, 109.0)]
    trade = simulate_trade(signal, future, signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0)
    assert trade is not None
    assert trade.entry_execution == 102.0
    assert trade.signal_rr == 2.0
    assert trade.actual_fill_rr == pytest.approx(8.0 / 7.0)
    assert trade.planned_rr == trade.signal_rr

    summary = summarize(
        days=1,
        period_start_ms=0,
        period_end_ms=86_400_000,
        coins_selected=1,
        coins_tested=1,
        data_errors=0,
        execution_errors=0,
        rejected_setups=0,
        trades=[trade],
    )
    assert summary.avg_signal_rr == pytest.approx(2.0)
    assert summary.avg_planned_rr == pytest.approx(2.0)
    assert summary.avg_actual_fill_rr == pytest.approx(8.0 / 7.0)
    assert "Avg Signal RR" in format_report(summary)
    assert "Avg Actual Fill RR" in format_report(summary)


def test_backtest_report_splits_take_profits_and_stop_losses_by_side():
    # Four independently simulated trades make every side/outcome bucket explicit.
    cases = [
        ({"symbol": "LONG_TP", "setup": "LONG", "entry": 100.0, "stop_loss": 95.0, "tp": 110.0}, candle(3_600_000, 100.0, 111.0, 99.0, 109.0), "TP"),
        ({"symbol": "LONG_SL", "setup": "LONG", "entry": 100.0, "stop_loss": 95.0, "tp": 110.0}, candle(3_600_000, 100.0, 101.0, 94.0, 96.0), "SL"),
        ({"symbol": "SHORT_TP", "setup": "SHORT", "entry": 100.0, "stop_loss": 105.0, "tp": 90.0}, candle(3_600_000, 100.0, 101.0, 89.0, 91.0), "TP"),
        ({"symbol": "SHORT_SL", "setup": "SHORT", "entry": 100.0, "stop_loss": 105.0, "tp": 90.0}, candle(3_600_000, 100.0, 106.0, 99.0, 104.0), "SL"),
    ]
    trades = []
    for signal, future, expected_outcome in cases:
        trade = simulate_trade(
            signal, [future], signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0
        )
        assert trade is not None
        assert trade.outcome == expected_outcome
        trades.append(trade)

    summary = summarize(
        days=1, period_start_ms=0, period_end_ms=86_400_000,
        coins_selected=4, coins_tested=4, data_errors=0, execution_errors=0,
        rejected_setups=0, trades=trades,
    )
    assert summary.tp_hits == 2
    assert summary.long_tp_hits == 1
    assert summary.short_tp_hits == 1
    assert summary.sl_hits == 2
    assert summary.long_sl_hits == 1
    assert summary.short_sl_hits == 1

    report = format_report(summary)
    assert "🎯 Total TP: 2" in report
    assert "LONG TPs: 1" in report
    assert "SHORT TPs: 1" in report
    assert "🛑 Total SL: 2" in report
    assert "LONG Trade SLs: 1" in report
    assert "SHORT Trade SLs: 1" in report


def test_side_tp_sl_breakdown_excludes_open_and_expired_trades():
    open_trade = simulate_trade(
        {"symbol": "OPEN_LONG", "setup": "LONG", "entry": 100.0, "stop_loss": 95.0, "tp": 110.0},
        [candle(3_600_000, 100.0, 102.0, 99.0, 101.0)],
        signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0,
    )
    expired_trade = simulate_trade(
        {"symbol": "EXPIRED_SHORT", "setup": "SHORT", "entry": 100.0, "stop_loss": 105.0, "tp": 90.0},
        [candle(3_600_000, 100.0, 102.0, 98.0, 101.0)],
        signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0,
        max_holding_minutes=30.0,
    )
    assert open_trade is not None and open_trade.outcome == "OPEN"
    assert expired_trade is not None and expired_trade.outcome == "EXPIRED"

    summary = summarize(
        days=1, period_start_ms=0, period_end_ms=86_400_000,
        coins_selected=2, coins_tested=2, data_errors=0, execution_errors=0,
        rejected_setups=0, trades=[open_trade, expired_trade],
    )
    assert summary.unresolved == 1
    assert summary.expired == 1
    assert summary.tp_hits == summary.sl_hits == 0
    assert summary.long_tp_hits == summary.short_tp_hits == 0
    assert summary.long_sl_hits == summary.short_sl_hits == 0
    # Incomplete OPEN trade excursions are not mixed into resolved-trade MAE/MFE.
    assert summary.avg_mae_r == pytest.approx(float(expired_trade.mae_r or 0.0))
    assert summary.avg_mfe_r == pytest.approx(float(expired_trade.mfe_r or 0.0))
    assert "LONG TPs: 0" in format_report(summary)
    assert "SHORT Trade SLs: 0" in format_report(summary)


def test_backtest_report_compares_measured_entry_quality_for_tp_and_sl():
    from app.backtest.simulator import SimulatedTrade

    common = dict(
        entry=100.0, stop_loss=95.0, tp1=110.0, tp2=110.0, planned_rr=2.0,
        signal_rr=2.0, actual_fill_rr=2.0, tp1_hit=False, tp2_hit=False,
        sl_hit=False, exit_time_ms=3_600_000, quality={
            "reclaim_body_ratio": 0.6, "reclaim_close_location": 0.8,
            "reclaim_rvol": 1.4, "entry_extension_atr": 0.3, "actual_entry_extension_atr": 0.4,
            "retest_depth_ratio": 0.5, "impulse_age_4h_bars": 4,
            "reclaim_bars_after_sweep": 2,
        },
    )
    tp_trade = SimulatedTrade(symbol="TP_USDT", side="LONG", signal_time_ms=0,
                              outcome="TP", r_multiple=1.8, **common)
    sl_trade = SimulatedTrade(symbol="SL_USDT", side="SHORT", signal_time_ms=1,
                              outcome="SL", r_multiple=-1.0, **{**common, "quality": {**common["quality"], "entry_extension_atr": 1.1}})
    summary = summarize(days=1, period_start_ms=0, period_end_ms=86_400_000,
                        coins_selected=2, coins_tested=2, data_errors=0,
                        execution_errors=0, rejected_setups=0, trades=[tp_trade, sl_trade])
    report = format_report(summary)
    assert summary.entry_quality_by_outcome["TP"]["sample_count"] == 1
    assert summary.entry_quality_by_outcome["SL"]["mean_entry_extension_atr"] == pytest.approx(1.1)
    assert "ENTRY QUALITY BY OUTCOME" in report
    assert "TP sample=1" in report
    assert "SL sample=1" in report
    assert "actual_fill_extension_ATR=0.40" in report


def test_simulator_records_actual_fill_extension_from_swept_liquidity():
    signal = {
        "symbol": "ABC_USDT", "setup": "LONG", "entry": 100.0,
        "stop_loss": 95.0, "tp": 115.0, "swept_level_1h": 99.0,
        "atr_1h": 2.0, "entry_mode": "MARKET",
    }
    trade = simulate_trade(signal, [candle(3_600_000, 102.0, 116.0, 101.0, 114.0)],
                           signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0)
    assert trade is not None
    assert trade.quality["actual_entry_extension_atr"] == pytest.approx(1.5)


def test_summary_drawdown_and_losing_streak_follow_realised_exit_order():
    from app.backtest.simulator import SimulatedTrade

    def trade(symbol, side, signal_ms, exit_ms, outcome, r_multiple):
        return SimulatedTrade(
            symbol=symbol, side=side, signal_time_ms=signal_ms,
            entry=100.0, stop_loss=95.0 if side == "LONG" else 105.0,
            tp1=110.0 if side == "LONG" else 90.0,
            tp2=110.0 if side == "LONG" else 90.0,
            planned_rr=2.0, signal_rr=2.0, actual_fill_rr=2.0,
            tp1_hit=outcome == "TP", tp2_hit=outcome == "TP", sl_hit=outcome == "SL",
            outcome=outcome, r_multiple=r_multiple, exit_time_ms=exit_ms,
        )

    # Signal-time order is -4R, +5R, -2R (4R drawdown); exit-time order is
    # +5R, -2R, -4R (6R drawdown). Realised-equity metrics must use exit order.
    a = trade("A_USDT", "LONG", 100, 300, "SL", -4.0)
    b = trade("B_USDT", "LONG", 200, 100, "TP", 5.0)
    c = trade("C_USDT", "SHORT", 300, 200, "SL", -2.0)
    summary = summarize(
        days=1, period_start_ms=0, period_end_ms=86_400_000,
        coins_selected=3, coins_tested=3, data_errors=0, execution_errors=0,
        rejected_setups=0, trades=[a, b, c],
    )
    assert summary.max_drawdown_r == pytest.approx(6.0)
    assert summary.max_losing_streak == 2


def test_backtest_runner_settings_preserve_explicit_zero_fees_and_slippage():
    from app.backtest.runner import _nonnegative_setting_float
    settings = Settings(backtest_fee_rate=0.0, backtest_slippage_bps=0.0)
    assert _nonnegative_setting_float(settings, "backtest_fee_rate", 0.0006) == 0.0
    assert _nonnegative_setting_float(settings, "backtest_slippage_bps", 2.0) == 0.0


def test_simulator_records_value_zone_distance_at_signal_and_actual_fill():
    signal = {
        "symbol": "ZONE_USDT", "setup": "LONG", "entry": 100.0,
        "stop_loss": 95.0, "tp": 110.0, "entry_mode": "MARKET",
        "atr_1h": 1.0, "swept_level_1h": 99.0,
        "value_zone_low": 98.0, "value_zone_high": 99.0,
        "value_zone_distance_atr": 1.0,
    }
    trade = simulate_trade(
        signal, [candle(3_600_000, 101.0, 111.0, 100.0, 109.0)],
        signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0,
    )
    assert trade is not None and trade.outcome == "TP"
    assert trade.quality["value_zone_distance_atr"] == pytest.approx(1.0)
    assert trade.quality["actual_value_zone_distance_atr"] == pytest.approx(2.0)
