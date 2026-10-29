from __future__ import annotations

import csv
import json

from app.backtest_rsi_ema import (
    RsiEmaBacktestConfig,
    render_equity_curve,
    run_backtest_rsi_ema,
    write_backtest_artifacts,
)
from app.market.candles import Candle, aggregate_3m_from_1m
from app.strategy.rsi_ema import RsiEmaConfig, evaluate_signals
import pytest


def _cross_fixture() -> list[Candle]:
    """Deterministic series that produces a LONG candidate without future data."""
    prices: list[float] = []
    price = 100.0
    for _ in range(150):
        price += 0.02
        prices.append(price)
    for drop in (1.0, 1.0, 1.0):
        price -= drop
        prices.append(price)
    for i in range(30):
        price += 0.3 * (1.2 ** min(i, 9))
        prices.append(price)
    bars: list[Candle] = []
    start = 1_704_067_200
    for i, close in enumerate(prices):
        open_price = prices[i - 1] if i else close - 0.02
        volume = 300.0 if i >= 153 else 100.0
        bars.append(Candle(
            symbol="TEST_USDT", timeframe="3M", open_time=start + i * 180,
            open=open_price, high=max(open_price, close) + 0.05,
            low=min(open_price, close) - 0.05, close=close,
            volume=volume, complete=True, source="synthetic-test-fixture",
        ))
    return bars


def test_3m_aggregation_needs_three_complete_aligned_contiguous_minutes():
    start = 1_704_067_200
    minutes = [
        Candle("BTC_USDT", "1M", start + i * 60, 100 + i, 101 + i, 99 + i, 100.5 + i,
               volume=10 + i, complete=True)
        for i in range(6)
    ]
    bars = aggregate_3m_from_1m(minutes)
    assert len(bars) == 2
    assert bars[0].open_time == start
    assert bars[0].open == minutes[0].open
    assert bars[0].close == minutes[2].close
    assert bars[0].high == max(x.high for x in minutes[:3])
    assert bars[0].low == min(x.low for x in minutes[:3])
    assert bars[0].volume == sum(x.volume for x in minutes[:3])
    missing_first_group = aggregate_3m_from_1m([minutes[0], minutes[2], *minutes[3:]])
    assert len(missing_first_group) == 1
    assert missing_first_group[0].open_time == start + 180
    incomplete_second_group = aggregate_3m_from_1m([minutes[0], minutes[1], minutes[2], minutes[3],
                                 Candle("BTC_USDT", "1M", start + 4*60, 104, 105, 103, 104.5, volume=1, complete=False),
                                 minutes[5]])
    assert len(incomplete_second_group) == 1
    assert incomplete_second_group[0].open_time == start


def test_rsi_ema_strategy_emits_only_long_after_crossover_and_overbought_rsi():
    bars = _cross_fixture()
    signals = evaluate_signals(bars)
    assert signals
    assert all(s.direction == "LONG" for s in signals)
    assert all(s.rsi >= 70 for s in signals)
    assert all(s.ema21 > s.ema50 > s.ema100 for s in signals)
    assert all(s.timestamp == bars[s.candle_index].close_time for s in signals)


def test_rsi_ema_strategy_has_no_future_candle_leakage():
    bars = _cross_fixture()
    earlier_end = 160
    prefix_signals = evaluate_signals(bars[:earlier_end + 1])
    full_signals = [s for s in evaluate_signals(bars) if s.candle_index <= earlier_end]
    assert [(s.timestamp, s.direction, s.close, s.rsi) for s in prefix_signals] == [
        (s.timestamp, s.direction, s.close, s.rsi) for s in full_signals
    ]


def test_rsi_ema_strategy_rejects_insufficient_warmup():
    start = 1_704_067_200
    bars = [Candle("BTC_USDT", "3M", start + i * 180, 100 + i, 101 + i, 99 + i,
                   100.5 + i, volume=100, complete=True) for i in range(80)]
    assert evaluate_signals(bars) == []


def test_empty_backtest_does_not_invent_trades_or_returns(tmp_path):
    report = run_backtest_rsi_ema({}, window_days=7, now_epoch=1_704_067_200,
                                  config=RsiEmaBacktestConfig())
    assert report["starting_equity"] == 100.0
    assert report["ending_equity"] == 100.0
    assert report["completed_trades"] == 0
    assert report["net_profit"] == 0.0
    assert report["win_rate_pct"] is None
    assert report["data_quality"]["symbols_with_bars"] == []

    paths = write_backtest_artifacts(report, tmp_path, prefix="empty_7d")
    assert json.loads((tmp_path / "empty_7d.json").read_text())["completed_trades"] == 0
    with open(paths["csv"], newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []
    curve = render_equity_curve(report, tmp_path / "empty_7d.png")
    assert curve.exists() and curve.stat().st_size > 1000


def test_backtest_uses_contract_quantity_rules_and_real_cost_fields():
    bars = _cross_fixture()
    now = bars[-1].close_time
    specs = {"TEST_USDT": {
        "contractSize": 1.0, "volUnit": 0.001, "minVol": 0.001,
        "maxVol": 10000, "maxLeverage": 20, "maintenanceMarginRate": 0.005,
    }}
    report = run_backtest_rsi_ema(
        {"TEST_USDT": bars}, window_days=1, now_epoch=now,
        contract_specs=specs,
        config=RsiEmaBacktestConfig(max_leverage=50),
    )
    assert report["short_count"] == 0
    assert report["max_concurrent_positions"] <= 1
    assert report["assumptions"]["max_leverage"] == 50
    assert report["data_quality"]["symbols_with_bars"] == ["TEST_USDT"]
    assert report["completed_trades"] == 1
    assert report["take_profit_exits"] == 1
    assert report["net_profit"] == pytest.approx(0.30, abs=1e-9)
    for trade in report["trade_ledger"]:
        assert trade["leverage"] <= 20
        assert trade["quantity"] >= 0.001
        assert trade["fees"] >= 0
        assert trade["estimated_slippage_cost"] >= 0
        assert trade["direction"] == "LONG"


def test_paper_sizing_respects_contract_leverage_and_quantity_precision():
    from app.paper_rsi_ema import PaperConfig, create_paper_position

    row = create_paper_position(
        symbol="TEST_USDT",
        signal={"direction": "LONG", "timestamp": 1_704_067_200, "reason": ["test"]},
        ticker={"lastPrice": "100.0"},
        contract={"contractSize": 1, "volUnit": 0.01, "minVol": 0.01,
                  "maxVol": 100, "countryConfigContractMaxLeverage": 10, "maxLeverage": 20,
                  "maintenanceMarginRate": 0.005},
        entry_time=1_704_067_200, equity=100,
        config=PaperConfig(max_leverage=50),
    )
    assert row["meta"]["leverage"] == 10
    assert row["quantity"] == 0.09
    assert row["meta"]["initial_margin"] <= 1.0
    assert row["meta"]["actual_live_order"] is False
    assert row["mode"] == "rsi_ema_paper"


def test_paper_exit_targets_net_profit_and_prioritizes_liquidation():
    from app.paper_rsi_ema import PaperConfig, create_paper_position, resolve_paper_exit

    start = 1_704_067_200
    pos = create_paper_position(
        symbol="TEST_USDT", signal={"direction": "LONG", "timestamp": start, "reason": ["test"]},
        ticker={"lastPrice": 100},
        contract={"contractSize": 1, "volUnit": 0.001, "minVol": 0.001,
                  "maxVol": 10000, "maxLeverage": 50, "maintenanceMarginRate": 0.005},
        entry_time=start, equity=100, config=PaperConfig(),
    )
    from app.market.candles import Candle
    target_hit = Candle("TEST_USDT", "3M", start, 100.05, pos["target_price"] + 0.2,
                        pos["meta"]["liquidation_price_estimate"] + 0.1, pos["target_price"],
                        volume=100, complete=True)
    out = resolve_paper_exit(pos, [target_hit], funding_events=[])
    assert out is not None
    assert out["reason"] == "TAKE_PROFIT_NET_TARGET"
    assert out["net_pnl"] == pytest.approx(0.30, abs=1e-8)

    liquidation_hit = Candle("TEST_USDT", "3M", start, 100.05, 100.2,
                             pos["meta"]["liquidation_price_estimate"] - 0.1,
                             pos["meta"]["liquidation_price_estimate"], volume=100, complete=True)
    out2 = resolve_paper_exit(pos, [liquidation_hit], funding_events=[])
    assert out2 is not None and out2["reason"] == "LIQUIDATION_ESTIMATE"


def test_paper_take_profit_waits_for_missing_funding_but_protective_exit_stays_active():
    from app.paper_rsi_ema import PaperConfig, create_paper_position, resolve_paper_exit
    from app.market.candles import Candle

    start = 1_704_067_200
    pos = create_paper_position(
        symbol="TEST_USDT", signal={"direction": "LONG", "timestamp": start, "reason": ["test"]},
        ticker={"lastPrice": 100},
        contract={"contractSize": 1, "volUnit": 0.001, "minVol": 0.001,
                  "maxVol": 10000, "maxLeverage": 50, "maintenanceMarginRate": 0.005},
        entry_time=start, equity=100, config=PaperConfig(),
    )
    target_bar = Candle("TEST_USDT", "3M", start, 100.05, pos["target_price"] + 0.2,
                        99.5, pos["target_price"], volume=100, complete=True)
    assert resolve_paper_exit(pos, [target_bar], funding_events=None) is None
    protective_bar = Candle("TEST_USDT", "3M", start, 100.05, 100.1,
                            pos["meta"]["liquidation_price_estimate"] - 0.1,
                            pos["meta"]["liquidation_price_estimate"], volume=100, complete=True)
    out = resolve_paper_exit(pos, [protective_bar], funding_events=None)
    assert out is not None and out["reason"] == "LIQUIDATION_ESTIMATE"


def test_all_contract_scanner_fails_closed_on_missing_status_or_sizing():
    from app.worker import _active_contract_rows

    good = {
        "symbol": "GOOD_USDT", "state": 0, "contractSize": 1,
        "volUnit": 0.001, "minVol": 0.001, "maxVol": 1000,
        "countryConfigContractMaxLeverage": 25,
    }
    no_state = {**good, "symbol": "NO_STATE_USDT"}
    del no_state["state"]
    bad_limits = {**good, "symbol": "BAD_LIMITS_USDT", "maxVol": 0}
    disabled = {**good, "symbol": "DISABLED_USDT", "state": 2}
    result = _active_contract_rows([good, no_state, bad_limits, disabled])
    assert list(result) == ["GOOD_USDT"]


def test_backtest_contract_validator_rejects_missing_or_inactive_metadata():
    from app.services.command_processor import _validate_backtest_contract_spec

    good = {"state": 0, "contractSize": 1, "volUnit": 0.01, "minVol": 0.01,
            "maxVol": 1000, "maxLeverage": 20}
    assert _validate_backtest_contract_spec(good) == (True, "OK")
    assert _validate_backtest_contract_spec({**good, "state": 1})[0] is False
    assert _validate_backtest_contract_spec({**good, "volUnit": 0})[0] is False
