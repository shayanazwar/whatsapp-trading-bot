from app.indicators import ema, rsi_wilder
from app.models import Candle
from app.strategy import StrategyConfig, evaluate, signal_at


def candle(t, o, h, l, c):
    return Candle(t, o, h, l, c, 1.0)


def test_ema_sma_seed_then_recursive():
    values = [1.0, 2.0, 3.0, 4.0]
    actual = ema(values, 3)
    assert actual[:2] == [None, None]
    assert actual[2] == 2.0
    assert actual[3] == 3.0


def test_rsi_wilder_initialization_and_edges():
    rising = rsi_wilder([1, 2, 3, 4, 5], 2)
    falling = rsi_wilder([5, 4, 3, 2, 1], 2)
    flat = rsi_wilder([5, 5, 5, 5, 5], 2)
    assert rising[2] == 100.0
    assert falling[2] == 0.0
    assert flat[2] == 50.0
    assert rising[0] is None


def test_signal_requires_all_long_conditions():
    candles = [candle(0, 99, 100, 98, 99), candle(60, 111, 113, 110, 112)]
    cfg = StrategyConfig(6, 12, 21, 9, 65, 35, 0.5)
    sig = signal_at(candles, 1, cfg,
                    [100, 108], [99, 105], [98, 102], [66, 67])
    assert sig is not None and sig.direction == "LONG"
    assert round(sig.tp_price, 3) == 112.56


def test_no_signal_if_ema_order_fails():
    candles = [candle(0, 99, 100, 98, 99), candle(60, 111, 113, 110, 112)]
    cfg = StrategyConfig(6, 12, 21, 9, 65, 35, 0.5)
    sig = signal_at(candles, 1, cfg,
                    [100, 108], [99, 109], [98, 102], [66, 67])
    assert sig is None


def test_no_signal_if_rsi_not_rising():
    candles = [candle(0, 99, 100, 98, 99), candle(60, 111, 113, 110, 112)]
    cfg = StrategyConfig(6, 12, 21, 9, 65, 35, 0.5)
    sig = signal_at(candles, 1, cfg,
                    [100, 108], [99, 105], [98, 102], [68, 67])
    assert sig is None


def test_evaluate_needs_sufficient_history():
    candles = [candle(i * 60, 1, 1, 1, 1) for i in range(10)]
    assert evaluate(candles, StrategyConfig()) is None
