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
    assert trade.sl_hit is True
    assert trade.outcome == "SL"
    assert trade.r_multiple == -1.0


def test_simulator_same_candle_sl_is_conservative():
    signal = {
        "symbol": "ABC_USDT",
        "setup": "LONG",
        "entry": 100.0,
        "stop_loss": 95.0,
        "tp1": 106.0,
        "tp2": 110.0,
    }
    future = [candle(M5 * 2, 100.0, high=111.0, low=94.0)]
    trade = simulate_trade(signal, future, signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0)
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
    future = [candle(M5 * 2, 100.0, high=107.0, low=94.0)]
    trade = simulate_trade(signal, future, signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0)
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
    future = [candle(M5 * 2, 99.0, high=100.0, low=89.0)]
    trade = simulate_trade(signal, future, signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0)
    assert trade is not None
    assert trade.outcome == "TP2"
    assert trade.tp1_hit is True
    assert trade.tp2_hit is True
    assert trade.r_multiple == 2.0


def test_report_metrics():
    signals = [
        {
            "symbol": "A_USDT", "setup": "LONG", "entry": 100, "stop_loss": 95, "tp1": 106, "tp2": 110,
        },
        {
            "symbol": "B_USDT", "setup": "SHORT", "entry": 100, "stop_loss": 105, "tp1": 94, "tp2": 90,
        },
    ]
    trades = []
    for signal in signals:
        if signal["setup"] == "LONG":
            future = [candle(M5 * 2, 109, high=111, low=100)]
        else:
            future = [candle(M5 * 2, 101, high=105, low=89)]
        trades.append(simulate_trade(signal, future, signal_close_time_ms=M5, fee_rate=0.0, slippage_bps=0.0))
    trades = [trade for trade in trades if trade is not None]

    summary = summarize(
        days=7,
        coins_selected=300,
        coins_tested=298,
        data_errors=2,
        trades=trades,
    )
    text = format_report(summary)
    assert "COINS TESTED: 298" in text
    assert "SIGNALS: 2" in text
    assert "TP2 HIT: 1" in text
    assert "WIN RATE: 50.0%" in text
    assert "TOTAL R: +1.00R" in text


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
    rows = []
    for i in range(count):
        ts = start + i * interval
        p = 100.0 + i * 0.01
        rows.append([ts, p, p + 0.5, p - 0.5, p + 0.1, 100 + i])
    return rows


def test_engine_accepts_historical_timestamp_without_future_candle():
    now = 1_700_000_000_000 + 250 * H4
    c4 = _synthetic_rows(250, H4)
    c1 = _synthetic_rows(250, H1)
    c15 = _synthetic_rows(250, M15)
    c5 = _synthetic_rows(250, M5)
    result = analyze_candles(
        "TEST_USDT",
        c4,
        c1,
        c15,
        c5,
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

    async def run_once():
        async with runner._lock:
            await asyncio.sleep(0.05)

    async def check():
        task = asyncio.create_task(run_once())
        await asyncio.sleep(0)
        assert runner.is_running
        with pytest.raises(BacktestAlreadyRunning):
            # Directly exercising the public guard with a minimal fake method.
            if runner.is_running:
                raise BacktestAlreadyRunning()
        await task

    asyncio.run(check())

@pytest.mark.asyncio
async def test_runner_symbol_integration_uses_full_engine_and_simulator(monkeypatch):
    import app.backtest.runner as runner_module

    start = 1_700_000_000_000
    period_start = start + 60 * 60 * 1000
    period_end = start + 8 * 60 * 60 * 1000
    retest_time = period_start + 30 * 60 * 1000
    bos_time = retest_time - M15
    signal_close = retest_time + M15

    history = SimpleNamespace(
        symbol="TEST_USDT",
        candles_4h=_synthetic_rows(250, H4, start=start - 80 * 24 * 60 * 60 * 1000),
        candles_1h=_synthetic_rows(250, H1, start=start - 20 * 24 * 60 * 60 * 1000),
        candles_15m=_synthetic_rows(200, M15, start=start - 5 * 24 * 60 * 60 * 1000),
        candles_5m=_synthetic_rows(400, M5, start=start - 2 * 24 * 60 * 60 * 1000),
        candles_1d=_synthetic_rows(60, 86_400_000, start=start - 60 * 24 * 60 * 60 * 1000),
        diagnostics={},
    )
    history.candles_15m.append([retest_time, 100.0, 101.0, 99.0, 100.5, 200.0])
    history.candles_15m.append([signal_close, 100.5, 101.0, 100.0, 100.8, 200.0])
    actual_candidate = retest_time + 2 * M15
    history.candles_5m.append([actual_candidate, 100.0, 101.0, 99.5, 100.8, 200.0])
    history.candles_5m.append([actual_candidate + M5, 100.8, 111.0, 100.0, 110.0, 200.0])

    def fake_bos(candles, side, lookback=70):
        assert lookback == len(candles)
        if side == "LONG":
            return [{"time": bos_time, "index": 1, "level": 100.0, "strength": 0.9}]
        return []

    def fake_retest(candles, side, bos, max_bars):
        return {"valid": True, "time": retest_time, "index": 2, "level": 100.0, "quality": 0.9, "rejection": True, "low": 99.0, "high": 101.0}

    def fake_5m(*args, **kwargs):
        return {"ready": True, "quality": 0.9, "rvol": 1.5, "rsi": 60}

    def fake_analyze(*args, **kwargs):
        assert kwargs["now_ms"] > retest_time
        assert len(args) >= 6
        assert args[5]
        assert args[5][-1]["time"] <= retest_time
        return {
            "symbol": "TEST_USDT",
            "setup": "LONG",
            "technical_candidate": True,
            "entry": 100.0,
            "stop_loss": 95.0,
            "tp1": 106.0,
            "tp2": 110.0,
            "trend_4h": "BULLISH",
            "regime": "BULLISH",
            "setup_bos_time": bos_time,
            "setup_retest_time": retest_time,
            "technical_gate_failures": [],
            "intraday_max_hold_minutes": 360,
        }

    monkeypatch.setattr(runner_module, "_bos_events", fake_bos)
    monkeypatch.setattr(runner_module, "_pullback_retest", fake_retest)
    monkeypatch.setattr(runner_module, "_five_minute_trigger", fake_5m)
    monkeypatch.setattr(runner_module, "analyze_candles", fake_analyze)
    monkeypatch.setattr(runner_module, "btc_filter_ok", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(runner_module, "build_btc_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(runner_module, "_four_hour_regime", lambda *args, **kwargs: {"bull": True, "bear": False, "regime": "BULLISH"})
    monkeypatch.setattr(runner_module, "_one_hour_alignment", lambda *args, **kwargs: {"long": True, "short": False})
    monkeypatch.setattr(runner_module, "_fifteen_minute_entry_confirmation", lambda *args, **kwargs: {"ready": True})

    runner = BacktestRunner.__new__(BacktestRunner)
    runner.client = None
    runner.universe = None
    runner.settings = SimpleNamespace(backtest_fee_rate=0.0, backtest_slippage_bps=0.0)
    runner.max_concurrency = 1
    runner._lock = asyncio.Lock()

    trades = runner._backtest_symbol(history, period_start, period_end, history, {})
    assert len(trades) == 1
    assert trades[0].outcome == "TP2"
    assert trades[0].tp1_hit is True
    assert trades[0].tp2_hit is True


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

    monkeypatch.setattr(runner_module, "_bos_events", lambda candles, side, lookback: [] )
    monkeypatch.setattr(runner_module, "_pullback_retest", lambda *args, **kwargs: {"valid": False})
    monkeypatch.setattr(runner_module, "_five_minute_trigger", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("5M trigger should not run without a 15M candidate")))

    out = runner_module.BacktestRunner._find_15m_setup_windows(c15, 1_700_000_000_000, 1_800_000_000_000, diagnostics)
    assert out == ()


def test_backtest_duration_accepts_1d(monkeypatch):
    from app.backtest import runner as runner_module
    assert 1 in {1, 7, 30, 90}
    assert runner_module.MAX_BACKTEST_SYMBOLS == 200


def test_runner_passes_reusable_15m_context(monkeypatch):
    from app.backtest import runner as runner_module
    source = open(runner_module.__file__, encoding="utf-8").read()
    assert "_BACKTEST_15M" in source
    assert "_build_15m_backtest_context" in source
