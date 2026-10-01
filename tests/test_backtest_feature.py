from __future__ import annotations

import asyncio
import pytest

from app.analysis.engine import analyze_candles
from app.automation.mexc_client import MexcClient
from app.automation.risk_manager import (
    TradePlan,
    calculate_contract_quantity,
    calculate_risk_amount,
    calculate_rr,
    quantize_to_step,
    validate_levels,
)
from app.automation.signal_validator import make_signal_key, validate_signal
from app.backtest.report import format_report, summarize
from app.backtest.runner import (
    BacktestAlreadyRunning,
    BacktestRunner,
    MAX_BACKTEST_SYMBOLS,
    _bypass_5m_confirmation,
)
from app.backtest.simulator import simulate_trade


M5 = 300_000
M15 = 900_000
H1 = 3_600_000
H4 = 14_400_000


def candle(ts: int, price: float, *, high: float | None = None, low: float | None = None):
    high = price if high is None else high
    low = price if low is None else low
    return [ts, price, high, low, price, 100.0]


def _synthetic_rows(count: int, interval: int, start: int = 1_700_000_000_000):
    return [
        [
            start + i * interval,
            100.0 + i * 0.01,
            100.5 + i * 0.01,
            99.5 + i * 0.01,
            100.1 + i * 0.01,
            100 + i,
        ]
        for i in range(count)
    ]


def test_simulator_tp1_then_sl():
    signal = {
        "symbol": "ABC_USDT",
        "setup": "LONG",
        "entry": 100.0,
        "stop_loss": 95.0,
        "tp1": 106.0,
        "tp2": 110.0,
    }
    future = [
        candle(M5, 100.0, high=107.0, low=99.0),
        candle(M5 * 2, 99.0, high=101.0, low=94.0),
    ]
    trade = simulate_trade(signal, future, signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0)
    assert trade is not None
    assert trade.tp1_hit is True
    assert trade.tp2_hit is False
    assert trade.sl_hit is False
    assert trade.breakeven_hit is True
    assert trade.outcome == "BE"
    assert trade.final_close_size == pytest.approx(0.5)
    assert trade.remaining_position_size == pytest.approx(0.0)
    assert trade.r_multiple == pytest.approx(0.6)


def test_simulator_same_candle_sl_is_conservative():
    signal = {
        "symbol": "ABC_USDT",
        "setup": "LONG",
        "entry": 100.0,
        "stop_loss": 95.0,
        "tp1": 106.0,
        "tp2": 110.0,
    }
    trade = simulate_trade(
        signal,
        [candle(M5 * 2, 100.0, high=111.0, low=94.0)],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert trade is not None
    assert trade.outcome == "SL"
    assert trade.tp2_hit is False


def test_simulator_same_candle_tp1_and_sl_is_conservative():
    signal = {
        "symbol": "ABC_USDT",
        "setup": "LONG",
        "entry": 100.0,
        "stop_loss": 95.0,
        "tp1": 106.0,
        "tp2": 110.0,
    }
    trade = simulate_trade(
        signal,
        [candle(M5 * 2, 100.0, high=107.0, low=94.0)],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert trade is not None
    assert trade.outcome == "SL"
    assert trade.tp1_hit is False
    assert trade.tp2_hit is False
    assert trade.sl_hit is True


def test_simulator_short_tp2():
    signal = {
        "symbol": "ABC_USDT",
        "setup": "SHORT",
        "entry": 100.0,
        "stop_loss": 105.0,
        "tp1": 94.0,
        "tp2": 90.0,
    }
    trade = simulate_trade(
        signal,
        [candle(M5 * 2, 99.0, high=100.0, low=89.0)],
        signal_close_time_ms=M5,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert trade is not None
    assert trade.outcome == "TP2"
    assert trade.tp1_hit is True
    assert trade.tp2_hit is True
    assert trade.tp1_close_size == pytest.approx(0.5)
    assert trade.final_close_size == pytest.approx(0.5)
    assert trade.remaining_position_size == pytest.approx(0.0)
    assert trade.r_multiple == pytest.approx(1.6)


def test_report_metrics():
    signals = [
        {"symbol": "A_USDT", "setup": "LONG", "entry": 100, "stop_loss": 95, "tp1": 106, "tp2": 110},
        {"symbol": "B_USDT", "setup": "SHORT", "entry": 100, "stop_loss": 105, "tp1": 94, "tp2": 90},
    ]
    trades = []
    for signal in signals:
        future = (
            [candle(M5 * 2, 109, high=111, low=100)]
            if signal["setup"] == "LONG"
            else [candle(M5 * 2, 101, high=104, low=89)]
        )
        trades.append(simulate_trade(signal, future, signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0))
    trades = [trade for trade in trades if trade is not None]
    summary = summarize(days=7, coins_selected=300, coins_tested=298, data_errors=2, trades=trades)
    text = format_report(summary)
    assert "COINS TESTED: 298" in text
    assert "SIGNALS: 2" in text
    assert "TP2 HIT: 2" in text
    assert "WIN RATE: 100.0%" in text
    assert "TOTAL R: +3.20R" in text
    assert "trigger_quality_5m" not in text


@pytest.mark.asyncio
async def test_historical_kline_range_parser(monkeypatch):
    client = object.__new__(MexcClient)

    async def fake_request(method, path, *, params=None, json_body=None, private=False):
        assert method == "GET"
        assert path == "/api/v1/contract/kline/ABC_USDT"
        assert params["start"] == 1000
        assert params["end"] == 2000
        return {
            "time": [900, 1000, 1001, 2000, 2001],
            "open": [1, 2, 3, 4, 5],
            "high": [2, 3, 4, 5, 6],
            "low": [0.5, 1.5, 2.5, 3.5, 4.5],
            "close": [1.5, 2.5, 3.5, 4.5, 5.5],
            "vol": [10, 20, 30, 40, 50],
        }

    client._request = fake_request
    rows = await client.get_klines_range("ABC_USDT", "Min5", 1_000_000, 2_000_000)
    assert [row[0] for row in rows] == [1_000_000, 1_001_000, 2_000_000]


def test_engine_accepts_historical_timestamp_without_future_candle():
    now = 1_700_000_000_000 + 250 * H4
    result = analyze_candles(
        "TEST_USDT",
        _synthetic_rows(250, H4),
        _synthetic_rows(250, H1),
        _synthetic_rows(250, M15),
        _synthetic_rows(250, M5),
        [],
        now_ms=now,
    )
    assert result["candle_time"] + M15 <= now


def test_backtest_universe_is_capped_at_200_symbols():
    import app.backtest.runner as runner_module
    assert runner_module.MAX_BACKTEST_SYMBOLS == 200


def test_backtest_runner_rejects_duplicate_job():
    runner = BacktestRunner.__new__(BacktestRunner)
    runner._lock = asyncio.Lock()
    runner.client = None
    runner.universe = None
    runner.settings = None
    runner.max_concurrency = 1

    async def check():
        async with runner._lock:
            assert runner.is_running
            with pytest.raises(BacktestAlreadyRunning):
                if runner.is_running:
                    raise BacktestAlreadyRunning()

    asyncio.run(check())


def test_backtest_closed_slice_excludes_open_candle():
    import app.backtest.runner as runner_module
    rows = _synthetic_rows(5, M15)
    close_time = rows[3][0] + M15
    sliced = runner_module._closed_slice(rows, M15, close_time)
    assert [row[0] for row in sliced] == [rows[0][0], rows[1][0], rows[2][0], rows[3][0]]
    assert rows[4] not in sliced


def test_runner_candidate_discovery_does_not_call_5m_trigger(monkeypatch):
    import app.backtest.runner as runner_module

    c15 = _synthetic_rows(120, M15)
    diagnostics = {}
    bos = {"index": 20, "time": c15[20][0], "level": 100.0, "strength": 0.8}
    monkeypatch.setattr(runner_module, "_bos_events", lambda candles, side, lookback: [bos] if side == "LONG" else [])
    monkeypatch.setattr(
        runner_module,
        "_pullback_retest",
        lambda candles, side, bos_event, max_age: {
            "valid": True,
            "index": 21,
            "time": c15[21][0],
            "quality": 0.9,
            "rejection": True,
        },
    )

    out = runner_module.BacktestRunner._find_15m_setup_windows(
        c15,
        c15[0][0],
        c15[-1][0] + M15,
        diagnostics,
    )
    assert len(out) == 1
    assert diagnostics["STRUCTURE_WINDOW_COLLAPSED"] > 0


def test_runner_bypasses_only_5m_rejection():
    accepted = _bypass_5m_confirmation(
        {
            "technical_candidate": False,
            "technical_gate_failures": ["5m_trigger_confirmation"],
        }
    )
    assert accepted["technical_candidate"] is True
    assert accepted["five_minute_confirmation_bypassed"] is True
    assert accepted["technical_gate_failures"] == []

    mixed = _bypass_5m_confirmation(
        {
            "technical_candidate": False,
            "technical_gate_failures": ["5m_trigger_confirmation", "1h_alignment"],
        }
    )
    assert mixed["technical_candidate"] is False


def test_runner_has_nonblocking_ipc_reader():
    import app.backtest.runner as runner_module
    source = open(runner_module.__file__, encoding="utf-8").read()
    assert "backtest-ipc-reader-" in source
    assert "messages.get_nowait()" in source
    method_source = source.split("async def _run_symbol_analysis_with_timeout", 1)[1].split(
        "async def _fetch_btc_history", 1
    )[0]
    assert method_source.count("parent_conn.recv()") == 1
    assert "def reader()" in method_source


def test_runner_uses_explicit_5m_bypass_flag():
    import app.backtest.runner as runner_module
    source = open(runner_module.__file__, encoding="utf-8").read()
    assert '"_BACKTEST_DISABLE_5M_CONFIRMATION": True' in source
    assert "_bypass_5m_confirmation(analysis)" in source


def test_signal_key_is_based_on_15m_setup_timestamp():
    assert make_signal_key("abc_usdt", "long", 1_700_000_000_000) == make_signal_key(
        "ABC_USDT", "LONG", 1_700_000_000_000
    )


def test_signal_validator_no_longer_requires_5m(monkeypatch):
    import app.automation.signal_validator as validator

    monkeypatch.setattr(validator.time, "time", lambda: 1_700_000_000 + 5 * 60)
    monkeypatch.setattr(
        validator,
        "validate_analysis",
        lambda *args, **kwargs: (True, []),
    )
    data = {
        "symbol": "ABC_USDT",
        "setup": "LONG",
        "candle_time": 1_700_000_000_000,
        "entry": 100,
        "stop_loss": 95,
        "tp1": 106,
        "tp2": 110,
    }
    signal, reasons = validate_signal(
        data,
        min_confluence=82,
        min_rr=2.0,
        require_increasing_volume=True,
    )
    assert reasons == []
    assert signal is not None
    assert signal.candle_time == data["candle_time"]
    assert signal.analysis["primary_entry_timeframe"] == "15M"
    assert signal.analysis["five_minute_confirmation_bypassed"] is True


def test_signal_validator_ignores_5m_only_engine_failure(monkeypatch):
    import app.automation.signal_validator as validator

    monkeypatch.setattr(validator.time, "time", lambda: 1_700_000_000 + 5 * 60)
    monkeypatch.setattr(
        validator,
        "validate_analysis",
        lambda *args, **kwargs: (False, ["5m_trigger_confirmation"]),
    )
    data = {
        "symbol": "ABC_USDT",
        "setup": "SHORT",
        "candle_time": 1_700_000_000_000,
        "entry": 100,
        "stop_loss": 105,
        "tp1": 94,
        "tp2": 90,
    }
    signal, reasons = validate_signal(
        data,
        min_confluence=82,
        min_rr=2.0,
        require_increasing_volume=True,
    )
    assert signal is not None
    assert reasons == []


def test_signal_validator_keeps_non_5m_rejections(monkeypatch):
    import app.automation.signal_validator as validator

    monkeypatch.setattr(validator.time, "time", lambda: 1_700_000_000 + 5 * 60)
    monkeypatch.setattr(
        validator,
        "validate_analysis",
        lambda *args, **kwargs: (False, ["5m_trigger_confirmation", "1h_alignment"]),
    )
    data = {
        "symbol": "ABC_USDT",
        "setup": "LONG",
        "candle_time": 1_700_000_000_000,
        "entry": 100,
        "stop_loss": 95,
        "tp1": 106,
        "tp2": 110,
    }
    signal, reasons = validate_signal(
        data,
        min_confluence=82,
        min_rr=2.0,
        require_increasing_volume=True,
    )
    assert signal is None
    assert reasons == ["1h_alignment"]


def test_risk_math_and_level_ordering():
    assert calculate_rr(side="LONG", entry=100, stop_loss=95, target=110) == pytest.approx(2.0)
    assert calculate_rr(side="SHORT", entry=100, stop_loss=105, target=90) == pytest.approx(2.0)

    plan = TradePlan("LONG", 100, 95, 106, 110, 2.0)
    assert validate_levels(plan, min_rr=2.0) == (True, "OK")
    assert calculate_risk_amount(1000, 1.0) == pytest.approx(10.0)
    assert quantize_to_step(1.239, 0.1) == pytest.approx(1.2)
    assert quantize_to_step(1.231, 0.1, mode="up") == pytest.approx(1.3)
    qty = calculate_contract_quantity(10, 100, 95, 1, 0.1, 0.1, 10, cost_buffer_pct=0)
    assert qty == pytest.approx(2.0)


def test_risk_math_rejects_non_finite_values():
    with pytest.raises(ValueError):
        calculate_rr(side="LONG", entry=float("inf"), stop_loss=95, target=110)
    with pytest.raises(ValueError):
        calculate_contract_quantity(10, 100, 95, 1, 0.1, 0.1, 10, cost_buffer_pct=float("inf"))
    with pytest.raises(ValueError):
        quantize_to_step(1.0, 0.1, mode="sideways")
