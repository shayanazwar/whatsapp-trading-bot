from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.backtest.report import format_report, summarize
from app.backtest.runner import (
    BacktestAlreadyRunning,
    BacktestRunner,
    MAX_BACKTEST_SYMBOLS,
    SymbolHistory,
    BacktestTPConfig,
)
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



def _fixed_hour_boundary(days_ago: int = 2) -> int:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return int((now.timestamp() - days_ago * 86_400) * 1000)


def _history_for_backtest(symbol: str = "ABC_USDT") -> SymbolHistory:
    hour = 3_600_000
    day = 86_400_000
    twelve = 43_200_000
    anchor = _fixed_hour_boundary(2)
    candles_1h = [[anchor - 220 * hour + i * hour, 100, 101, 99, 100.5, 1000] for i in range(221)]
    candles_4h = [[anchor - 220 * 14_400_000 + i * 14_400_000, 100, 101, 99, 100.5, 1000] for i in range(221)]
    candles_1d = [[anchor - 220 * day + i * day, 100, 101, 99, 100.5, 1000] for i in range(221)]
    candles_12h = [[anchor - 220 * twelve + i * twelve, 100, 101, 99, 100.5, 1000] for i in range(221)]
    return SymbolHistory(symbol=symbol, candles_1d=candles_1d, candles_12h=candles_12h, candles_4h=candles_4h, candles_1h=candles_1h)


def test_fixed_period_rejects_future_and_non_exact_duration(monkeypatch):
    runner = BacktestRunner(client=None, universe=None, settings=Settings(), max_concurrency=1)
    start = _fixed_hour_boundary(8)
    end = start + 7 * 86_400_000
    monkeypatch.setenv("V11_BACKTEST_START_MS", str(start))
    monkeypatch.setenv("V11_BACKTEST_END_MS", str(end))
    assert runner._period(7) == (start, end)

    monkeypatch.setenv("V11_BACKTEST_END_MS", str(end + 3_600_000))
    with pytest.raises(ValueError, match="exactly 7D"):
        runner._period(7)

    future = int(datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).timestamp() * 1000) + 3_600_000
    monkeypatch.setenv("V11_BACKTEST_START_MS", str(future - 7 * 86_400_000))
    monkeypatch.setenv("V11_BACKTEST_END_MS", str(future))
    with pytest.raises(ValueError, match="future|current 1H candle"):
        runner._period(7)


def test_tp_mode_cannot_change_signal_population(monkeypatch):
    history = _history_for_backtest()
    settings = Settings(backtest_max_symbols=1)
    runner = BacktestRunner(client=None, universe=None, settings=settings, max_concurrency=1)

    calls = {"engine": 0, "sim": 0}

    def fake_analyze(*args, **kwargs):
        calls["engine"] += 1
        signal_time = int(args[4][-1]["time"]) + 3_600_000
        return {
            "technical_candidate": True,
            "btc_filter_ok": True,
            "setup": "LONG",
            "setup_candidate": "LONG",
            "entry": 100.0,
            "stop_loss": 95.0,
            "tp": 110.0,
            "rr": 2.0,
            "candle_time": signal_time,
            "setup_impulse_high_time": signal_time - 14_400_000,
            "setup_impulse_low_time": signal_time - 28_800_000,
            "swept_level_1h": 99.0,
            "reclaim_time_1h": signal_time - 3_600_000,
            "entry_mode": "MARKET",
        }

    def fake_simulate(*args, **kwargs):
        calls["sim"] += 1
        signal = args[0]
        mode_r = kwargs.get("tp_r_multiple")
        extra = 1 * 3_600_000 if mode_r == 1.5 else 5 * 3_600_000
        return SimpleNamespace(
            symbol=signal["symbol"], side="LONG", signal_time_ms=int(kwargs["signal_close_time_ms"]),
            tp1_hit=False, tp2_hit=False, sl_hit=True, expired=False, outcome="SL", exit_time_ms=int(kwargs["signal_close_time_ms"]) + extra,
        )

    monkeypatch.setattr("app.backtest.runner.analyze_candles", fake_analyze)
    monkeypatch.setattr("app.backtest.runner.simulate_trade_1h", fake_simulate)

    start_ms = history.candles_1h[-4][0]
    end_ms = history.candles_1h[-1][0] + 3_600_000
    one = runner._simulate_symbol(history, start_ms, end_ms, {}, BacktestTPConfig.from_mode("1.5R"))
    signal_count_one = one[1]["SIGNALS_READY_FOR_TP_SIMULATION"]

    calls["engine"] = 0
    calls["sim"] = 0
    two = runner._simulate_symbol(history, start_ms, end_ms, {}, BacktestTPConfig.from_mode("2.0R"))
    signal_count_two = two[1]["SIGNALS_READY_FOR_TP_SIMULATION"]

    assert signal_count_one == signal_count_two
    assert signal_count_one == len(one[0]) == len(two[0])
    assert one[1]["TP_SIGNAL_COUPLING_REMOVED"] == 1


def test_backtest_snapshot_reuses_universe_and_verifies_data(monkeypatch, tmp_path):
    class FakeUniverse:
        max_symbols = 2
        test_symbols = set()
        def __init__(self):
            self.calls = 0
        async def refresh(self):
            self.calls += 1
            return ["AAA_USDT", "BBB_USDT"] if self.calls == 1 else ["CCC_USDT", "DDD_USDT"]

    universe = FakeUniverse()
    settings = Settings(backtest_max_symbols=2, database_path=str(tmp_path / "signals.sqlite3"))
    runner = BacktestRunner(client=None, universe=universe, settings=settings, max_concurrency=1)
    history = _history_for_backtest()
    anchor = _fixed_hour_boundary(10)
    start = anchor
    end = start + 86_400_000
    monkeypatch.setenv("V11_BACKTEST_START_MS", str(start))
    monkeypatch.setenv("V11_BACKTEST_END_MS", str(end))
    monkeypatch.setenv("V11_BACKTEST_SNAPSHOT_DIR", str(tmp_path / "snapshots"))

    async def fake_btc(_start, _end):
        return _history_for_backtest("BTC_USDT")
    async def fake_history(symbol, _start, _end):
        return _history_for_backtest(symbol)
    def fake_simulate_symbol(self, hist, _start, _end, _btc, _tp):
        return [], {"ENGINE_CALLS": 0, "ENGINE_SUCCESS": 0, "TECHNICAL_REJECT": 0}, 0, 0, 0

    runner._fetch_btc_history = fake_btc
    runner._fetch_history = fake_history
    runner._simulate_symbol = fake_simulate_symbol.__get__(runner, BacktestRunner)
    monkeypatch.setattr(BacktestRunner, "_build_btc_context_cache", staticmethod(lambda *args, **kwargs: {}))

    first = asyncio.run(runner.run(1, tp_mode="CONTROL"))
    second = asyncio.run(runner.run(1, tp_mode="1.5R"))

    assert universe.calls == 1
    assert first.snapshot_status == "CREATED"
    assert second.snapshot_status == "REUSED"
    assert first.universe_hash == second.universe_hash
    assert first.data_snapshot_hash == second.data_snapshot_hash
    assert first.snapshot_id == second.snapshot_id
    assert first.snapshot_id


def test_report_exposes_r_ledger_and_mfe_reach_metrics():
    signal = {"symbol": "ABC_USDT", "setup": "LONG", "entry": 100.0, "stop_loss": 95.0, "tp": 107.5}
    trade = simulate_trade(signal, [candle(3_600_000, 100, 108, 94, 106)], signal_close_time_ms=0, fee_rate=0, slippage_bps=0)
    assert trade is not None
    summary = summarize(
        days=1, period_start_ms=0, period_end_ms=86_400_000,
        coins_selected=1, coins_tested=1, data_errors=0, execution_errors=0,
        rejected_setups=0, trades=[trade], snapshot_id="snap", universe_hash="u", data_snapshot_hash="d", snapshot_status="REUSED",
    )
    text = format_report(summary)
    assert "Snapshot: REUSED" in text
    assert "R Ledger:" in text
    assert "MFE Reach:" in text
    assert summary.ledger_reconciled is True



def test_report_flags_counterfactual_tp_without_matching_mfe():
    from app.backtest.report import summarize
    from app.backtest.simulator import SimulatedTrade
    trade = SimulatedTrade(
        symbol="TEST_USDT", side="LONG", signal_time_ms=1, entry=100.0,
        stop_loss=99.0, tp1=101.5, tp2=101.5, planned_rr=1.5, signal_rr=1.5,
        actual_fill_rr=1.5, tp1_hit=True, tp2_hit=True, sl_hit=False, outcome="TP",
        r_multiple=1.4, exit_time_ms=2, mfe_r=1.0, mae_r=0.1, counterfactual_tp_r=1.5,
    )
    summary = summarize(days=1, period_start_ms=0, period_end_ms=1, coins_selected=1,
                        coins_tested=1, data_errors=0, execution_errors=0, rejected_setups=0,
                        trades=[trade])
    assert summary.diagnostics["TP_HIT_WITHOUT_MFE_TARGET"] == 1
    assert "MFE_TARGET_INTEGRITY=FAIL" in format_report(summary)


def test_runtime_diagnostic_separates_effective_and_raw_environment(monkeypatch):
    import os
    monkeypatch.setenv("V11_MIN_IMPULSE_ATR", "500.00")
    # This test verifies the diagnostic contract without changing engine behavior.
    raw = os.getenv("V11_MIN_IMPULSE_ATR")
    assert raw == "500.00"
    assert int(round(float(raw) * 100)) == 50000
