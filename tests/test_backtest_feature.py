from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from app.backtest.report import format_report, summarize
from app.backtest.runner import (
    BacktestAlreadyRunning, BacktestRunner, MAX_BACKTEST_SYMBOLS, SymbolHistory,
    _normalize_failure_diagnostic_key,
)
from app.backtest.reproducibility import BacktestReproStore
from app.analysis.engine import convert_candles
from app.backtest.simulator import simulate_trade
from app.automation.mexc_client import MexcClient
from app.config import Settings


def candle(ts, o, h, l, c, v=1000.0):
    return [ts, o, h, l, c, v]



def test_short_failure_diagnostics_group_volatile_numeric_values():
    assert _normalize_failure_diagnostic_key(
        "SHORT: 4H impulse invalidated above origin 1.242"
    ) == "SHORT_4H_IMPULSE_INVALIDATED_ABOVE_ORIGIN"
    assert _normalize_failure_diagnostic_key(
        "SHORT: post-cost RR 0.52 < required 1.60"
    ) == "SHORT_POST-COST_RR_BELOW_REQUIRED"
    assert _normalize_failure_diagnostic_key(
        "SHORT: SL distance 3.82 ATR outside 0.20-3.00 safety bounds"
    ) == "SHORT_SL_DISTANCE_OUTSIDE_SAFETY_BOUNDS"


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


def test_simulator_applies_configured_funding_reserve_to_realized_r():
    signal = {
        "symbol": "ABC_USDT", "setup": "LONG", "entry": 100.0,
        "stop_loss": 95.0, "tp": 110.0,
    }
    future = [candle(3_600_000, 100.0, 111.0, 99.0, 108.0)]
    without_reserve = simulate_trade(
        signal, future, signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0,
    )
    with_reserve = simulate_trade(
        signal, future, signal_close_time_ms=0, fee_rate=0.0, slippage_bps=0.0,
        funding_cost_pct=0.01,
    )
    assert without_reserve is not None and with_reserve is not None
    assert with_reserve.funding_cost == pytest.approx(1.0)
    assert with_reserve.funding_r == pytest.approx(0.2)
    assert with_reserve.realized_pnl == pytest.approx(without_reserve.realized_pnl - 1.0)
    assert with_reserve.r_multiple == pytest.approx(without_reserve.r_multiple - 0.2)


def test_backtest_short_shadow_is_opt_in_and_cost_model_is_conservative():
    settings = Settings()
    assert settings.backtest_short_shadow_enabled is False
    assert settings.effective_round_trip_cost_pct == pytest.approx(0.0018)
    enabled = Settings(V11_BACKTEST_SHORT_SHADOW_ENABLED=True)
    assert enabled.backtest_short_shadow_enabled is True


def test_short_shadow_report_labels_outcomes_as_non_portfolio_samples():
    summary = summarize(
        days=7,
        period_start_ms=0,
        period_end_ms=7 * 86_400_000,
        coins_selected=1,
        coins_tested=1,
        data_errors=0,
        execution_errors=0,
        rejected_setups=0,
        trades=[],
        diagnostics={
            "SHORT_SHADOW_MODE_ENABLED": 1,
            "SHORT_SHADOW_DAILY_PERMISSION_DENIED_EVALUATIONS": 100,
            "SHORT_SHADOW_SETUP_CANDIDATES": 2,
            "SHORT_SHADOW_SIMULATED_TRADES": 2,
            "SHORT_SHADOW_RESOLVED_TRADES": 2,
            "SHORT_SHADOW_OUTCOME_TP": 1,
            "SHORT_SHADOW_OUTCOME_SL": 1,
            "SHORT_SHADOW_TOTAL_REALIZED_R_X1000": 500,
            "SHORT_SHADOW_POSITIVE_R_X1000": 2000,
            "SHORT_SHADOW_NEGATIVE_R_ABS_X1000": 1500,
        },
    )
    report = format_report(summary)
    assert "SHORT daily-gate shadow: ON (research-only; not added to portfolio trades)" in report
    assert "These overlapping counterfactual outcomes are NOT a portfolio backtest." in report
    assert "Win rate (TP / TP+SL): 50.0%" in report
    assert "Total R / expectancy: +0.500R / 0.250R/trade" in report


def test_report_prints_full_short_failure_and_overlap_diagnostics():
    summary = summarize(
        days=7,
        period_start_ms=0,
        period_end_ms=7 * 86_400_000,
        coins_selected=1,
        coins_tested=1,
        data_errors=0,
        execution_errors=0,
        rejected_setups=100,
        trades=[],
        diagnostics={
            "TP_MODE_X100": 150,
            "SHORT_SHADOW_MODE_ENABLED": 1,
            "ENGINE_CALLS": 10_000,
            "FIRST_FAILURE_SHORT_TOTAL": 100,
            "FIRST_FAILURE_SHORT_SHORT_1D_TREND_PERMISSION_UNAVAILABLE": 70,
            "FIRST_FAILURE_SHORT_SHORT_HOSTILE_12H_CONTEXT": 30,
            "OVERLAP_DIAGNOSTIC_EVALUATIONS": 40,
            "OVERLAP_SHORT_SIDE_CANDIDATES": 3,
            "OVERLAP_FIRST_FAILURE_SHORT_SHORT_HOSTILE_12H_CONTEXT": 12,
        },
    )
    report = format_report(summary)

    # These detailed counters must print even when outside the global top 20.
    assert "SHORT FIRST-FAILURE HISTOGRAM" in report
    assert "FIRST_FAILURE_SHORT_SHORT_1D_TREND_PERMISSION_UNAVAILABLE: 70" in report
    assert "FIRST_FAILURE_SHORT_SHORT_HOSTILE_12H_CONTEXT: 30" in report
    assert "ACTIVE-POSITION OVERLAP DIAGNOSTICS" in report
    assert "Overlap closes checked: 40 | Strict SHORT side candidates found: 3" in report
    assert "OVERLAP_FIRST_FAILURE_SHORT_SHORT_HOSTILE_12H_CONTEXT: 12" in report
    assert "Shadow exit target: matches main TP mode (1.5R)." in report
    assert "V11_SHORT_RANGE_RELAXED is metadata-only" in report
    assert "Histogram reconciliation: categorized=100 | first_failures=100 | difference=0" in report


def test_frozen_snapshot_round_trip_preserves_normalized_candles_and_universe(tmp_path):
    start_ms = 1_790_780_400_000
    end_ms = 1_791_385_200_000
    candles = convert_candles([
        [1_790_000_000_000, 100.0, 105.0, 98.0, 102.0, 123.0],
        [1_790_003_600_000, 102.0, 106.0, 101.0, 104.0, 145.0],
    ])
    history = SymbolHistory(
        symbol="ABC_USDT",
        candles_1d=candles,
        candles_12h=candles,
        candles_4h=candles,
        candles_1h=candles,
    )
    btc = SymbolHistory(
        symbol="BTC_USDT",
        candles_1d=candles,
        candles_12h=candles,
        candles_4h=candles,
        candles_1h=candles,
    )
    store = BacktestReproStore.capture(str(tmp_path), start_ms=start_ms, end_ms=end_ms)
    store.set_symbols(["ABC_USDT"])
    store.write_history(history, role="symbol")
    store.write_history(btc, role="btc_context")
    manifest = store.finalize(
        start_ms=start_ms,
        end_ms=end_ms,
        symbols=["ABC_USDT"],
        engine_config={"fingerprint": "test-fingerprint", "min_impulse_atr": 2.5},
        settings=Settings(),
        tp_mode="CONTROL",
        project_root=pathlib.Path.cwd(),
    )
    assert manifest["complete"] is True
    replay = BacktestReproStore.replay(str(store.path), start_ms=start_ms, end_ms=end_ms)
    restored = replay.load_history("ABC_USDT", role="symbol")
    assert restored.symbol == "ABC_USDT"
    assert restored.times_1h == history.times_1h
    assert restored.candles_1h == history.candles_1h
    assert replay.symbols == ["ABC_USDT"]


def test_frozen_snapshot_refuses_tampered_candle_data(tmp_path):
    start_ms = 1_790_780_400_000
    end_ms = 1_791_385_200_000
    candles = convert_candles([
        [1_790_000_000_000, 100.0, 105.0, 98.0, 102.0, 123.0],
    ])
    history = SymbolHistory(
        symbol="ABC_USDT",
        candles_1d=candles,
        candles_12h=candles,
        candles_4h=candles,
        candles_1h=candles,
    )
    store = BacktestReproStore.capture(str(tmp_path), start_ms=start_ms, end_ms=end_ms)
    store.set_symbols(["ABC_USDT"])
    store.write_history(history, role="symbol")
    store.write_history(SymbolHistory("BTC_USDT", candles, candles, candles, candles), role="btc_context")
    store.finalize(
        start_ms=start_ms,
        end_ms=end_ms,
        symbols=["ABC_USDT"],
        engine_config={"fingerprint": "test-fingerprint"},
        settings=Settings(),
        tp_mode="CONTROL",
        project_root=pathlib.Path.cwd(),
    )
    file_path = store.path / store.manifest["history_files"]["symbol:ABC_USDT"]["path"]
    file_path.write_bytes(file_path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="checksum mismatch"):
        BacktestReproStore.replay(str(store.path), start_ms=start_ms, end_ms=end_ms)


def test_replay_mode_bypasses_live_universe_and_exchange_fetches(tmp_path, monkeypatch):
    start_ms = 1_790_780_400_000
    end_ms = 1_791_385_200_000
    monkeypatch.setenv("V11_BACKTEST_START_MS", str(start_ms))
    monkeypatch.setenv("V11_BACKTEST_END_MS", str(end_ms))
    monkeypatch.delenv("V11_BACKTEST_SNAPSHOT_DIR", raising=False)
    snapshot = BacktestReproStore.capture(str(tmp_path), start_ms=start_ms, end_ms=end_ms)
    snapshot.set_symbols(["ABC_USDT"])
    empty = SymbolHistory("ABC_USDT", [], [], [], [])
    btc = SymbolHistory("BTC_USDT", [], [], [], [])
    snapshot.write_history(empty, role="symbol")
    snapshot.write_history(btc, role="btc_context")
    snapshot.finalize(
        start_ms=start_ms,
        end_ms=end_ms,
        symbols=["ABC_USDT"],
        engine_config={"fingerprint": "test-fingerprint"},
        settings=Settings(),
        tp_mode="CONTROL",
        project_root=pathlib.Path.cwd(),
    )
    monkeypatch.setenv("V11_BACKTEST_REPLAY_DIR", str(snapshot.path))

    class ForbiddenUniverse:
        async def refresh(self):
            raise AssertionError("Replay must not query the live universe")

    runner = BacktestRunner(
        client=None,
        universe=ForbiddenUniverse(),
        settings=Settings(backtest_max_symbols=1),
        max_concurrency=1,
    )
    summaries = [asyncio.run(runner.run(7, tp_mode="CONTROL")) for _ in range(3)]
    assert all(summary.coins_selected == 1 for summary in summaries)
    assert all(summary.coins_tested == 1 for summary in summaries)
    assert all(summary.signals == 0 for summary in summaries)
    run_files = sorted((snapshot.path / "results").glob("run_CONTROL_*.json"))
    assert len(run_files) == 3
    run_records = [json.loads(path.read_text(encoding="utf-8")) for path in run_files]
    assert len({record["trade_ledger_sha256"] for record in run_records}) == 1
    assert len({record["source_sha256"] for record in run_records}) == 1
