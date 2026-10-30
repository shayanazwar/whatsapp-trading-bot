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
        "priceUnit": 0.01, "takerFeeRate": 0.0008, "liquidationFeeRate": 0.0002,
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
    assert report["net_profit"] == pytest.approx(0.29925, abs=0.001)
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
            "maxVol": 1000, "maxLeverage": 20, "priceUnit": 0.01,
            "maintenanceMarginRate": 0.005, "quoteCoin": "USDT", "settleCoin": "USDT",
            "futureType": 1, "positionOpenType": 3}
    assert _validate_backtest_contract_spec(good) == (True, "OK")
    assert _validate_backtest_contract_spec({**good, "state": 1})[0] is False
    assert _validate_backtest_contract_spec({**good, "volUnit": 0})[0] is False
    assert _validate_backtest_contract_spec({**good, "positionOpenType": 1}) == (False, "CONTRACT_CROSS_MARGIN_UNSUPPORTED")
    assert _validate_backtest_contract_spec({**good, "futureType": 2}) == (False, "CONTRACT_NOT_USDT_PERPETUAL")
    assert _validate_backtest_contract_spec({**good, "futureType": "invalid"}) == (False, "CONTRACT_FUTURE_TYPE_INVALID")
    missing_mode = {key: value for key, value in good.items() if key != "positionOpenType"}
    assert _validate_backtest_contract_spec(missing_mode) == (False, "CONTRACT_METADATA_MISSING:positionOpenType")


def test_rsi_ema_strategy_emits_short_signals_for_mirrored_downtrend():
    from dataclasses import replace

    mirrored = [
        replace(bar, open=300 - bar.open, high=300 - bar.low,
                low=300 - bar.high, close=300 - bar.close)
        for bar in _cross_fixture()
    ]
    signals = evaluate_signals(mirrored)
    assert signals
    assert all(signal.direction == "SHORT" for signal in signals)
    assert all(signal.rsi <= 30 for signal in signals)
    assert all(signal.ema21 < signal.ema50 < signal.ema100 for signal in signals)


def test_cross_margin_liquidation_uses_shared_wallet_not_initial_margin():
    from app.backtest_rsi_ema import _cross_liquidation_price

    # $100 cross wallet, $50 position value (0.5 units at $100), 0.5% MMR.
    long_cross = _cross_liquidation_price(
        direction="LONG", entry_price=100, unit_quantity=0.5,
        wallet_balance=100, maintenance_rate=0.005,
        liquidation_fee_rate=0.0, isolated_margin=0.0,
    )
    short_cross = _cross_liquidation_price(
        direction="SHORT", entry_price=100, unit_quantity=0.5,
        wallet_balance=100, maintenance_rate=0.005,
        liquidation_fee_rate=0.0, isolated_margin=0.0,
    )
    long_isolated = _cross_liquidation_price(
        direction="LONG", entry_price=100, unit_quantity=0.5,
        wallet_balance=100, maintenance_rate=0.005,
        liquidation_fee_rate=0.0, isolated_margin=1.0,
    )

    assert long_cross is None  # no positive-price liquidation level in this simple setup
    assert short_cross == pytest.approx(299.5)
    assert long_isolated == pytest.approx(98.50)

    # MEXC's published cross-margin example: 1 BTC at 8,000, 500 USDT wallet,
    # 0.5% maintenance margin -> 7,540 USDT liquidation price (fee set to zero).
    mexc_example = _cross_liquidation_price(
        direction="LONG", entry_price=8000, unit_quantity=1,
        wallet_balance=500, maintenance_rate=0.005,
        liquidation_fee_rate=0.0, isolated_margin=0.0,
    )
    assert mexc_example == pytest.approx(7540.0)


def test_backtest_r_metrics_use_net_planned_stop_risk_and_report_schema():
    bars = _cross_fixture()
    report = run_backtest_rsi_ema(
        {"TEST_USDT": bars}, window_days=1, now_epoch=bars[-1].close_time,
        contract_specs={"TEST_USDT": {
            "contractSize": 1.0, "volUnit": 0.001, "minVol": 0.001,
            "maxVol": 10000, "maxLeverage": 20, "maintenanceMarginRate": 0.005,
            "priceUnit": 0.01, "takerFeeRate": 0.0008, "liquidationFeeRate": 0.0002,
        }},
        config=RsiEmaBacktestConfig(),
    )
    trade = report["trade_ledger"][0]
    assert report["long_count"] == report["long_signals"]
    assert report["short_count"] == report["short_signals"]
    assert trade["realized_r"] == pytest.approx(trade["net_pnl"] / trade["planned_net_stop_loss"])
    assert trade["planned_net_stop_loss"] == pytest.approx(3.0, abs=0.01)
    assert trade["planned_net_take_profit"] == pytest.approx(0.30, abs=0.01)
    assert report["margin_mode"] == "cross"
    assert report["assumptions"]["wallet_balance_includes_shared_cross_collateral"] is True


def test_requested_whatsapp_report_contains_direction_and_risk_statistics():
    from app.backtest_rsi_ema import format_backtest_whatsapp

    bars = _cross_fixture()
    report = run_backtest_rsi_ema(
        {"TEST_USDT": bars}, window_days=1, now_epoch=bars[-1].close_time,
        contract_specs={"TEST_USDT": {
            "contractSize": 1.0, "volUnit": 0.001, "minVol": 0.001,
            "maxVol": 10000, "maxLeverage": 20, "maintenanceMarginRate": 0.005,
            "priceUnit": 0.01, "takerFeeRate": 0.0008, "liquidationFeeRate": 0.0002,
        }},
        config=RsiEmaBacktestConfig(),
    )
    text = format_backtest_whatsapp(report, elapsed_seconds=1.2)
    for required in (
        "💥BACKTEST 1D 💥", "🪙 Coins Tested:", "📡 Signals:", "🟢 LONG:",
        "🔴 SHORT:", "✅ Resolved:", "⏳ Open/Unresolved:", "🎯 Total TP:",
        "🛑 Total SL:", "⌛ Expired:", "📈 Win Rate:", "⚖️ Avg Signal RR:",
        "🎯 Avg Actual Fill RR:", "💰 Avg Realized R:", "💰 Total R:",
        "📊 Expectancy:", "📐 Profit Factor:", "📉 Max Drawdown:",
        "📉 Max Losing Streak:", "🧭 Avg MAE / MFE:", "⚙️ Entry Mode:",
    ):
        assert required in text


def test_mexc_risk_increase_tier_applies_mmr_and_leverage_increments():
    from app.backtest_rsi_ema import _risk_tier_terms

    terms = _risk_tier_terms(
        {
            "riskLimitMode": "INCREASE", "riskLimitType": "BY_VOLUME",
            "riskBaseVol": 100, "riskIncrVol": 100, "riskIncrMmr": 0.002,
            "riskIncrImr": 0.01, "riskLevelLimit": 3, "initialMarginRate": 0.02,
        },
        direction="LONG", quantity=150, notional=75, base_mmr=0.005,
        contract_max_leverage=50,
    )
    assert terms["status"] == "VERIFIED"
    assert terms["tier"] == 2
    assert terms["mmr"] == pytest.approx(0.007)
    assert terms["max_leverage"] == 33


def test_mexc_custom_risk_tier_rejects_position_above_all_limits():
    from app.backtest_rsi_ema import _risk_tier_terms

    terms = _risk_tier_terms(
        {"riskLimitMode": "CUSTOM", "riskLimitType": "BY_VALUE",
         "riskLimitCustom": [{"level": 1, "maxVol": 40, "mmr": 0.005, "imr": 0.02, "maxLeverage": 50}]},
        direction="SHORT", quantity=2, notional=50, base_mmr=0.005,
        contract_max_leverage=50,
    )
    assert terms["status"] == "OVER_LIMIT"


def test_positive_funding_charged_to_long_credited_to_short_at_fair_price():
    from app.backtest_rsi_ema import _funding_events_for_trade

    start = 1_704_067_200
    fair_bar = Candle(
        symbol="TEST_USDT", timeframe="1M", open_time=start + 3600 - 60,
        open=110, high=112, low=109, close=111, volume=1, complete=True,
        source="mexc_fair_price_rest",
    )
    event = {"symbol": "TEST_USDT", "settleTime": start + 3600, "fundingRate": 0.001}
    long_cost = _funding_events_for_trade(
        [event], "TEST_USDT", start, start + 7200, 50, 0, "LONG",
        quantity_units=0.5, fair_price_bars=[fair_bar],
    )
    short_cost = _funding_events_for_trade(
        [event], "TEST_USDT", start, start + 7200, 50, 0, "SHORT",
        quantity_units=0.5, fair_price_bars=[fair_bar],
    )
    assert long_cost == pytest.approx(0.0555)
    assert short_cost == pytest.approx(-0.0555)


def test_cross_wallet_prevents_false_liquidation_before_configured_stop():
    from dataclasses import replace

    bars = _cross_fixture()
    # Signal at bar 160 enters at bar 161. Force price through the -$3 stop,
    # but still far above the long cross-liquidation level supported by $100 wallet.
    bars[161] = replace(bars[161], low=80.0)
    fair_bars = [replace(bar, source="mexc_fair_price_rest") for bar in bars]
    specs = {"TEST_USDT": {
        "contractSize": 1.0, "volUnit": 0.001, "minVol": 0.001,
        "maxVol": 10000, "maxLeverage": 20, "maintenanceMarginRate": 0.005,
        "priceUnit": 0.01, "takerFeeRate": 0.0008, "liquidationFeeRate": 0.0002,
    }}
    report = run_backtest_rsi_ema(
        {"TEST_USDT": bars}, window_days=1, now_epoch=bars[-1].close_time,
        contract_specs=specs,
        liquidation_candles_by_symbol={"TEST_USDT": fair_bars},
        config=RsiEmaBacktestConfig(starting_equity=100, initial_margin=1, margin_mode="cross"),
    )
    trade = report["trade_ledger"][0]
    assert trade["exit_reason"] == "STOP_LOSS"
    assert report["liquidation_exits"] == 0
    assert report["stop_loss_exits"] == 1
    assert trade["liquidation_price_estimate"] is None
