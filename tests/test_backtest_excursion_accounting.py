from __future__ import annotations

import pytest

from app.backtest.simulator import simulate_trade

ONE_HOUR = 3_600_000


def candle(ts, open_, high, low, close, volume=1000.0):
    return [ts, open_, high, low, close, volume]


def signal(*, side="LONG", entry=100.0, stop=95.0, tp=110.0):
    return {
        "symbol": "ABC_USDT",
        "setup": side,
        "entry": entry,
        "stop_loss": stop,
        "tp": tp,
    }


def test_stop_exit_bar_does_not_claim_favourable_excursion_after_stop():
    # The bar touches the stop and rallies to +1.4R. OHLC cannot establish
    # whether that rally happened before or after the stop, so it is not counted.
    trade = simulate_trade(
        signal(),
        [candle(ONE_HOUR, 100.0, 107.0, 94.0, 103.0)],
        signal_close_time_ms=0,
        fee_rate=0.0,
        slippage_bps=0.0,
    )

    assert trade is not None
    assert trade.outcome == "SL"
    assert trade.r_multiple == pytest.approx(-1.0)
    assert trade.mae_r == pytest.approx(1.0)
    assert trade.mfe_r == pytest.approx(0.0)
    assert trade.mfe_1r_hit is False
    assert trade.mfe_1_5r_hit is False


def test_stop_exit_bar_preserves_excursions_from_prior_completed_bars_only():
    trade = simulate_trade(
        signal(),
        [
            candle(ONE_HOUR, 100.0, 104.0, 98.0, 102.0),  # +0.8R MFE, 0.4R MAE
            candle(2 * ONE_HOUR, 102.0, 107.0, 94.0, 100.0),  # stop; high order unknown
        ],
        signal_close_time_ms=0,
        fee_rate=0.0,
        slippage_bps=0.0,
    )

    assert trade is not None
    assert trade.outcome == "SL"
    assert trade.mae_r == pytest.approx(1.0)
    assert trade.mfe_r == pytest.approx(0.8)
    assert trade.mfe_1r_hit is False


def test_target_exit_counts_known_target_mfe_but_not_unknown_exit_bar_adverse_extreme():
    trade = simulate_trade(
        signal(),
        [candle(ONE_HOUR, 100.0, 111.0, 96.0, 108.0)],
        signal_close_time_ms=0,
        fee_rate=0.0,
        slippage_bps=0.0,
    )

    assert trade is not None
    assert trade.outcome == "TP"
    assert trade.mfe_r == pytest.approx(2.0)
    assert trade.mfe_1r_hit is True
    assert trade.mfe_1_5r_hit is True
    # The final bar's low may have occurred after the TP exit; do not infer it.
    assert trade.mae_r == pytest.approx(0.0)


def test_gap_through_stop_records_known_gap_distance_separately_from_slippage():
    trade = simulate_trade(
        signal(),
        [
            candle(ONE_HOUR, 100.0, 102.0, 99.0, 101.0),
            candle(2 * ONE_HOUR, 93.0, 101.0, 90.0, 95.0),
        ],
        signal_close_time_ms=0,
        fee_rate=0.0,
        slippage_bps=0.0,
    )

    assert trade is not None
    assert trade.outcome == "SL"
    assert trade.mae_r == pytest.approx(1.4)  # entry 100 -> gap open 93; risk 5
    assert trade.mfe_r == pytest.approx(0.4)  # only the pre-gap completed bar


def test_same_bar_sl_first_does_not_mark_tp_excursion_as_pre_stop_favourable_move():
    trade = simulate_trade(
        signal(),
        [candle(ONE_HOUR, 100.0, 111.0, 94.0, 100.0)],
        signal_close_time_ms=0,
        fee_rate=0.0,
        slippage_bps=0.0,
        same_bar_rule="SL_FIRST",
    )

    assert trade is not None
    assert trade.outcome == "SL"
    assert trade.mfe_r == pytest.approx(0.0)
    assert trade.mfe_1r_hit is False
    assert trade.mfe_1_5r_hit is False
