from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.analysis.engine import analyze_candles
from app.automation.mexc_client import MexcClient
from app.backtest.report import format_report, summarize
from app.backtest.runner import BacktestAlreadyRunning, BacktestRunner, MAX_BACKTEST_SYMBOLS
from app.backtest.simulator import simulate_trade


M5 = 300_000
M15 = 900_000
H1 = 3_600_000
H4 = 14_400_000


def candle(ts: int, price: float, *, high: float | None = None, low: float | None = None):
    high = price if high is None else high
    low = price if low is None else low
    return [ts, price, high, low, price, 100.0]


def test_simulator_tp1_then_sl():
    signal = {
        "symbol": "ABC_USDT", "setup": "LONG", "entry": 100.0,
        "stop_loss": 95.0, "tp1": 106.0, "tp2": 110.0,
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
        "symbol": "ABC_USDT", "setup": "LONG", "entry": 100.0,
        "stop_loss": 95.0, "tp1": 106.0, "tp2": 110.0,
    }
    trade = simulate_trade(
        signal, [candle(M5 * 2, 100.0, high=111.0, low=94.0)],
        signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0,
    )
    assert trade is not None
    assert trade.outcome == "SL"
    assert trade.tp2_hit is False


def test_simulator_same_candle_tp1_and_sl_is_conservative():
    signal = {
        "symbol": "ABC_USDT", "setup": "LONG", "entry": 100.0,
        "stop_loss": 95.0, "tp1": 106.0, "tp2": 110.0,
    }
    trade = simulate_trade(
        signal, [candle(M5 * 2, 100.0, high=107.0, low=94.0)],
        signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0,
    )
    assert trade is not None
    assert trade.outcome == "SL"
    assert trade.tp1_hit is False
    assert trade.tp2_hit is False
    assert trade.sl_hit is True


def test_simulator_short_tp2():
    signal = {
        "symbol": "ABC_USDT", "setup": "SHORT", "entry": 100.0,
        "stop_loss": 105.0, "tp1": 94.0, "tp2": 90.0,
    }
    trade = simulate_trade(
        signal, [candle(M5 * 2, 99.0, high=100.0, low=89.0)],
        signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0,
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


def _synthetic_rows(count: int, interval: int, start: int = 1_700_000_000_000):
    return [
        [start + i * interval, 100.0 + i * 0.01, 100.5 + i * 0.01, 99.5 + i * 0.01, 100.1 + i * 0.01, 100 + i]
        for i in range(count)
    ]


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


def test_runner_prefilter_skips_5m_trigger_when_no_15m_candidate(monkeypatch):
    import app.backtest.runner as runner_module
    c15 = _synthetic_rows(120, M15)
    diagnostics = {}
    monkeypatch.setattr(runner_module, "_bos_events", lambda candles, side, lookback: [])
    monkeypatch.setattr(runner_module, "_pullback_retest", lambda *args, **kwargs: {"valid": False})
    monkeypatch.setattr(
        runner_module,
        "_five_minute_trigger",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("5M trigger should not run without a 15M candidate")
        ),
    )
    out = runner_module.BacktestRunner._find_15m_setup_windows(
        c15, 1_700_000_000_000, 1_800_000_000_000, diagnostics
    )
    assert out == ()


def test_runner_has_nonblocking_ipc_reader():
    import app.backtest.runner as runner_module
    source = open(runner_module.__file__, encoding="utf-8").read()
    assert "backtest-ipc-reader-" in source
    assert "messages.get_nowait()" in source
    # The asyncio parent loop must never perform a blocking Pipe.recv().
    method_source = source.split("async def _run_symbol_analysis_with_timeout", 1)[1].split(
        "async def _fetch_btc_history", 1
    )[0]
    assert method_source.count("parent_conn.recv()") == 1
    assert "def reader()" in method_source


def test_runner_passes_reusable_15m_context():
    import app.backtest.runner as runner_module
    source = open(runner_module.__file__, encoding="utf-8").read()
    assert "_BACKTEST_15M" in source
    assert "_build_15m_backtest_context" in source
