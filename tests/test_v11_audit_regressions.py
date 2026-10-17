from __future__ import annotations

import asyncio

import pytest

from app.analysis import engine
from app.backtest.report import format_report, summarize
from app.backtest.runner import BacktestRunner, _apply_portfolio_caps, _period
from app.backtest.simulator import SimulatedTrade
from app.config import Settings

DAY = 86_400_000
HOUR = 3_600_000


def _trade(
    symbol: str,
    side: str,
    signal_ms: int,
    exit_ms: int,
    *,
    control_exit_ms: int | None = None,
) -> SimulatedTrade:
    return SimulatedTrade(
        symbol=symbol,
        side=side,
        signal_time_ms=signal_ms,
        entry=100.0,
        stop_loss=95.0 if side == "LONG" else 105.0,
        tp1=110.0 if side == "LONG" else 90.0,
        tp2=110.0 if side == "LONG" else 90.0,
        planned_rr=2.0,
        signal_rr=2.0,
        actual_fill_rr=2.0,
        tp1_hit=False,
        tp2_hit=False,
        sl_hit=False,
        outcome="EXPIRED",
        r_multiple=0.0,
        exit_time_ms=exit_ms,
        control_exit_time_ms=control_exit_ms,
    )


def test_backtest_window_is_frozen_from_settings_and_normalizes_seconds():
    settings = Settings(
        backtest_start_ms=1_791_133_200,
        backtest_end_ms=1_791_738_000,
    )
    assert _period(7, settings) == (1_791_133_200_000, 1_791_738_000_000)


def test_runner_accepts_explicit_frozen_window_without_network_calls():
    class EmptyUniverse:
        async def refresh(self):
            return []

    runner = BacktestRunner(client=None, universe=EmptyUniverse(), settings=Settings(), max_concurrency=1)
    summary = asyncio.run(runner.run(7, start_ms=1_791_133_200, end_ms=1_791_738_000))
    assert summary.period_start_ms == 1_791_133_200_000
    assert summary.period_end_ms == 1_791_738_000_000
    report = format_report(summary)
    assert "V11_BACKTEST_START_MS=1791133200000" in report
    assert "V11_BACKTEST_END_MS=1791738000000" in report


def test_1d_bearish_permission_is_directional_and_not_gifted_by_adx(monkeypatch):
    rows = [
        {"time": i * DAY, "open": 99.0, "high": 101.0, "low": 97.0, "close": 99.0, "volume": 1000.0}
        for i in range(220)
    ]
    monkeypatch.setattr(engine, "_safe_ema", lambda values, period: {21: 102.0, 50: 101.0, 200: 100.0}[period])
    monkeypatch.setattr(engine, "_ema_slope", lambda *args, **kwargs: -0.01)
    monkeypatch.setattr(engine, "_adx", lambda *args, **kwargs: 20.0)
    monkeypatch.setattr(engine, "_v11_structure", lambda *args, **kwargs: "LH/LL")

    # Price is just below EMA200 but EMA50 still above EMA200: the lagging EMA
    # must not cancel active bearish structure/slope when the directional votes agree.
    rows[-1]["close"] = 99.0
    result = engine._v11_regime_1d(engine.convert_candles(rows))
    assert result["bear"] is True
    assert result["bull"] is False
    assert result["bear_votes"] > result["bull_votes"]


def test_target_selection_uses_real_structure_and_never_synthesizes_a_capped_price():
    candles = []
    for i in range(21):
        high = 110.0 if i == 10 else 101.0
        candles.append({
            "time": (1_700_000_000_000 + i * HOUR),
            "open": 100.0, "high": high, "low": 99.0, "close": 100.0, "volume": 1000.0,
        })
    impulse = {"low": 80.0, "high": 120.0, "high_time": candles[10]["time"], "low_time": candles[2]["time"]}
    result = engine._v11_target(engine.convert_candles(candles), [], [], [], 100.0, 4.0, "LONG", impulse, 0.001)
    assert result["ok"] is True
    assert result["timeframe"] == "1H"
    assert result["price"] == pytest.approx(110.0)
    assert result["rr_gross"] <= engine.V11_MAX_TARGET_RR


def test_portfolio_acceptance_uses_control_exit_for_all_tp_modes():
    settings = Settings(
        backtest_max_open_positions=1,
        backtest_max_same_direction=1,
        backtest_total_open_risk_r=3.0,
    )
    # Same accepted signal has a short counterfactual exit, but its control exit
    # remains later than the second signal, so both modes must reject that second entry.
    control_a = _trade("A_USDT", "LONG", 100, 300, control_exit_ms=300)
    tp_a = _trade("A_USDT", "LONG", 100, 150, control_exit_ms=300)
    b = _trade("B_USDT", "SHORT", 200, 500, control_exit_ms=500)

    accepted_control, skipped_control = _apply_portfolio_caps([control_a, b], settings)
    accepted_tp, skipped_tp = _apply_portfolio_caps([tp_a, b], settings)
    assert [t.symbol for t in accepted_control] == ["A_USDT"]
    assert [t.symbol for t in accepted_tp] == ["A_USDT"]
    assert skipped_control == skipped_tp == 1


def test_config_metadata_is_run_level_not_symbol_aggregated():
    source = open("app/backtest/runner.py", encoding="utf-8").read()
    per_symbol_block = source[source.index("def _simulate_symbol("):source.index("async def _heartbeat(")]
    assert 'diagnostics["V11_MIN_IMPULSE_ATR_X100"]' not in per_symbol_block
    assert 'diagnostics["V11_MAX_TRIGGER_BARS"]' not in per_symbol_block
    assert 'diagnostics["V11_MIN_IMPULSE_ATR_X100"]' in source[source.index('state["phase"] = "FINALIZING"'):]


def test_200_symbol_backtest_does_not_inflate_config_diagnostics():
    class Fixed200Universe:
        async def refresh(self):
            return [f"COIN{i}_USDT" for i in range(200)]

    runner = BacktestRunner(client=None, universe=Fixed200Universe(), settings=Settings(), max_concurrency=4)

    async def fake_fetch(symbol, start_ms, end_ms):
        from app.backtest.runner import SymbolHistory
        return SymbolHistory(symbol=symbol, candles_1d=[], candles_12h=[], candles_4h=[], candles_1h=[])

    runner._fetch_history = fake_fetch
    runner._fetch_btc_history = lambda s, e: fake_fetch("BTC_USDT", s, e)
    summary = asyncio.run(runner.run(1, start_ms=1_791_727_200, end_ms=1_791_813_600))
    assert summary.coins_tested == 200
    assert summary.diagnostics["V11_MIN_IMPULSE_ATR_X100"] == 250
    assert summary.diagnostics["V11_MAX_TRIGGER_BARS"] == 6
    report = format_report(summary)
    assert "500.00ATR" not in report
    assert "1200B" not in report
    assert summary.diagnostics["V11_ACCURACY_MIN_TRIGGER_RVOL_X100"] == 0


def test_configured_backtest_symbols_override_dynamic_universe():
    class FailingUniverse:
        async def refresh(self):
            raise AssertionError("Dynamic universe refresh must not be called when backtest_symbols is configured")

    settings = Settings(backtest_symbols="BTC_USDT,ETH_USDT")
    runner = BacktestRunner(client=None, universe=FailingUniverse(), settings=settings, max_concurrency=1)

    async def fake_fetch(symbol, start_ms, end_ms):
        from app.backtest.runner import SymbolHistory
        return SymbolHistory(symbol=symbol, candles_1d=[], candles_12h=[], candles_4h=[], candles_1h=[])

    runner._fetch_history = fake_fetch
    runner._fetch_btc_history = lambda s, e: fake_fetch("BTC_USDT", s, e)
    summary = asyncio.run(runner.run(7, start_ms=1_791_208_800, end_ms=1_791_813_600))
    assert summary.coins_selected == 2
    assert summary.coins_tested == 2


def test_counterfactual_trade_preserves_control_exit_timestamp():
    from app.backtest.simulator import simulate_trade
    signal = {
        "symbol": "SOL_USDT",
        "setup": "LONG",
        "entry": 100.0,
        "stop_loss": 95.0,
        "tp": 115.0,
        "entry_mode": "MARKET",
    }
    future = [
        [1_000 * HOUR, 100.0, 108.5, 99.0, 108.0, 1000.0],  # Hits 1.5R (107.5), not 3.0R (115.0)
        [1_001 * HOUR, 108.0, 116.0, 107.0, 115.5, 1000.0],  # Hits CONTROL TP (115.0)
    ]
    ctrl = simulate_trade(signal, future, signal_close_time_ms=1_000 * HOUR, fee_rate=0.0, slippage_bps=0.0)
    cf15 = simulate_trade(signal, future, signal_close_time_ms=1_000 * HOUR, fee_rate=0.0, slippage_bps=0.0, counterfactual_tp_r=1.5)
    assert ctrl is not None and cf15 is not None
    assert cf15.exit_time_ms == 1_001 * HOUR
    assert ctrl.exit_time_ms == 1_002 * HOUR
    assert cf15.control_exit_time_ms == ctrl.exit_time_ms
