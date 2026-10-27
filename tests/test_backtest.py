from app.backtest import BacktestConfig, SimPosition, _close_position, run_backtest, settle_fixed_payout
from app.market.candles import aggregate_12h_from_4h
from tests.helpers import candles


def test_fixed_payout_accounting_is_exact_and_does_not_deduct_costs_twice():
    balance, pnl, r = settle_fixed_payout(100, True, 20, 5)
    assert (balance, pnl, r) == (120, 20, 4)
    balance, pnl, r = settle_fixed_payout(balance, False, 20, 5)
    assert (balance, pnl, r) == (115, -5, -1)


def test_fixed_payout_closed_trade_uses_adverse_same_bar_resolution():
    pos = SimPosition("BTC_USDT", "LONG", 0, 1704067200, 100, 98, 105, "C1", "S1", 5, mode="fixed_payout")
    candle = candles("BTC_USDT", "1H", 1704067200, 1, 3600, base=101)[0]
    # Force a bar whose high and low both pass target and stop; stop must be chosen.
    from app.market.candles import Candle
    ambiguous = Candle("BTC_USDT", "1H", 1704067200, 100, 106, 97, 102, volume=1)
    trade = _close_position(pos, ambiguous, 98, "STOP_AMBIGUOUS_BAR", BacktestConfig(), 0.0008, 0.0005, None)
    assert trade.pnl == -5
    assert trade.exit_reason == "STOP_AMBIGUOUS_BAR"


def test_empty_backtest_does_not_fabricate_win_rate():
    result = run_backtest({}, window="7D", config=BacktestConfig())
    assert result["completed_trades"] == 0
    assert result["win_rate_pct"] is None
    assert result["ending_balance"] == 100


def test_real_market_window_with_flat_data_reports_no_edge_or_win_rate():
    day_start = 1704067200  # UTC midnight
    end = day_start + 220 * 86400
    daily = candles("BTC_USDT", "1D", day_start, 220, 86400, base=100, trend=0)
    h4_start = end - 180 * 14400
    bars4 = candles("BTC_USDT", "4H", h4_start, 180, 14400, base=100, trend=0)
    h1_start = end - 48 * 3600
    bars1 = candles("BTC_USDT", "1H", h1_start, 48, 3600, base=100, trend=0)
    h12 = aggregate_12h_from_4h(bars4)
    result = run_backtest({"BTC_USDT": {"1D": daily, "12H": h12, "4H": bars4, "1H": bars1}},
                          window="1D", config=BacktestConfig(), now_epoch=end)
    assert result["symbols_eligible"] == 1
    assert result["completed_trades"] == 0
    assert result["win_rate_pct"] is None
    assert result["ending_balance"] == 100
    assert any("hypothetical" in warning.lower() for warning in result["warnings"])


def test_funding_accrues_once_as_timeline_advances():
    from app.backtest import _accrue_funding_through
    from app.market.candles import Candle

    start = 1_704_067_200
    pos = SimPosition(
        "BTC_USDT", "LONG", 0, start, 100.0, 95.0, 110.0, "C1", "S1", 10.0,
        quantity=10.0, contract_size=1.0, mode="exchange_realistic", funding_checked_until=start,
    )
    bar = Candle("BTC_USDT", "1H", start, 100.0, 112.0, 99.0, 110.0, volume=1.0)
    events = {"BTC_USDT": [{"settleTime": str((start + 3600) * 1000), "fundingRate": 0.001}]}

    _accrue_funding_through(pos, bar, bar.close_time, events)
    first = pos.funding_pnl
    _accrue_funding_through(pos, bar, bar.close_time, events)

    assert first == -1.1
    assert pos.funding_pnl == first
    assert pos.funding_checked_until == bar.close_time


def test_ambiguous_exit_bar_charges_adverse_funding_but_not_favorable_funding():
    from app.backtest import _accrue_funding_through
    from app.market.candles import Candle

    start = 1_704_067_200
    bar = Candle("BTC_USDT", "1H", start, 100.0, 112.0, 90.0, 105.0, volume=1.0)
    favorable = SimPosition(
        "BTC_USDT", "LONG", 0, start, 100.0, 95.0, 110.0, "C1", "S1", 10.0,
        quantity=10.0, contract_size=1.0, mode="exchange_realistic", funding_checked_until=start,
    )
    adverse = SimPosition(
        "BTC_USDT", "LONG", 0, start, 100.0, 95.0, 110.0, "C2", "S2", 10.0,
        quantity=10.0, contract_size=1.0, mode="exchange_realistic", funding_checked_until=start,
    )

    _accrue_funding_through(favorable, bar, bar.close_time,
                            {"BTC_USDT": [{"settleTime": bar.close_time, "fundingRate": -0.001}]},
                            allow_favorable=False)
    _accrue_funding_through(adverse, bar, bar.close_time,
                            {"BTC_USDT": [{"settleTime": bar.close_time, "fundingRate": 0.001}]},
                            allow_favorable=False)

    assert favorable.funding_pnl == 0.0
    assert adverse.funding_pnl < 0.0
