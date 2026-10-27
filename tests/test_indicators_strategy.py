import pytest

from app.market.candles import Candle, CandleDataError
from app.market.indicators import atr, confirmed_pivots, ema, rsi
from app.strategy.rfpc import StrategyInputError, evaluate_strategy
from tests.helpers import candles


def test_ema_uses_sma_seed_and_returns_none_before_warmup():
    result = ema([1, 2, 3, 4, 5], 3)
    assert result[:2] == [None, None]
    assert result[2] == 2
    assert result[3] == pytest.approx(3)


def test_rsi_constant_series_is_neutral_not_nan():
    result = rsi([100.0] * 30, 14)
    assert result[14] == 50
    assert result[-1] == 50


def test_atr_length_and_nonnegative_values():
    bars = candles("BTC_USDT", "1H", 1704067200, 20, 3600)
    result = atr([x.high for x in bars], [x.low for x in bars], [x.close for x in bars], 14)
    assert len(result) == len(bars)
    assert all(x is None for x in result[:13])
    assert result[-1] is not None and result[-1] >= 0


def test_confirmed_pivot_indices_never_include_unconfirmed_right_edge():
    highs = [1, 2, 5, 2, 1, 2, 3]
    lows = [5, 4, 1, 4, 5, 4, 3]
    ph, pl = confirmed_pivots(highs, lows, 2)
    assert 2 in ph
    assert 2 in pl
    assert max(ph) <= len(highs) - 3
    assert max(pl) <= len(lows) - 3


def test_strategy_rejects_forbidden_timeframes():
    data = {"1D": [], "12H": [], "4H": [], "1H": [], "5M": []}
    with pytest.raises(CandleDataError):
        evaluate_strategy("BTC_USDT", data, decision_time=1704067200)


def test_strategy_requires_complete_timeframe_contract():
    with pytest.raises(StrategyInputError):
        evaluate_strategy("BTC_USDT", {"1D": [], "4H": [], "1H": []}, decision_time=1704067200)


def test_strategy_never_emits_signal_with_inadequate_daily_history():
    start = 1704067200
    daily = candles("BTC_USDT", "1D", start, 40, 86400, trend=0.1)
    h4 = candles("BTC_USDT", "4H", start + 40*86400, 60, 14400, trend=0.05)
    h12 = []
    for c in range(0, len(h4), 3):
        trio = h4[c:c+3]
        if len(trio) == 3 and trio[0].open_time % 43200 == 0:
            from app.market.candles import aggregate_12h_from_4h
            h12.extend(aggregate_12h_from_4h(trio))
    h1 = candles("BTC_USDT", "1H", start + 40*86400, 120, 3600, trend=0.02)
    end = h1[-1].close_time
    ev = evaluate_strategy("BTC_USDT", {"1D": daily, "12H": h12, "4H": h4, "1H": h1}, decision_time=end)
    assert ev.candidate is None
    assert "EMA_WARMUP_INSUFFICIENT" in ev.rejection_reasons


def test_prevalidated_backtest_path_matches_defensive_strategy_path():
    from app.market.candles import aggregate_12h_from_4h

    start = 1704067200  # UTC midnight, divisible by 12 hours
    daily = candles("BTC_USDT", "1D", start, 240, 86400, base=100, trend=0.08)
    h4_start = start + 150 * 86400
    h4 = candles("BTC_USDT", "4H", h4_start, 540, 14400, base=100, trend=0.025)
    h12 = aggregate_12h_from_4h(h4)
    h1_start = start + 210 * 86400
    h1 = candles("BTC_USDT", "1H", h1_start, 600, 3600, base=100, trend=0.01)
    decision_time = h1[-1].close_time
    data = {
        "1D": [c for c in daily if c.complete and c.close_time <= decision_time],
        "12H": [c for c in h12 if c.complete and c.close_time <= decision_time],
        "4H": [c for c in h4 if c.complete and c.close_time <= decision_time],
        "1H": [c for c in h1 if c.complete and c.close_time <= decision_time],
    }
    defensive = evaluate_strategy("BTC_USDT", data, decision_time=decision_time)
    fast = evaluate_strategy(
        "BTC_USDT", data, decision_time=decision_time, prevalidated=True
    )
    assert fast == defensive
