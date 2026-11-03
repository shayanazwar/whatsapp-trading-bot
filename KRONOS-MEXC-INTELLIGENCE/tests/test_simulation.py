import numpy as np
import pytest
from app.simulation.barrier import race_one_path, evaluate_paths
from app.simulation.bootstrap import moving_block_bootstrap_ohlc


def test_barrier_long_tp2_before_sl():
    out = race_one_path(np.array([[100, 111, 99, 110]], dtype=float), "LONG", 100, 95, 105, 110)
    assert out.outcome == "TP2_REACHED_BEFORE_SL"


def test_barrier_short_sl_first():
    out = race_one_path(np.array([[100, 106, 94, 101]], dtype=float), "SHORT", 100, 105, 95, 90)
    assert out.outcome == "AMBIGUOUS_CONSERVATIVE_STOP"


def test_same_bar_ambiguous_conservative():
    out = race_one_path(np.array([[100, 106, 94, 100]], dtype=float), "LONG", 100, 95, 105, 110)
    assert out.outcome == "AMBIGUOUS_CONSERVATIVE_STOP"


def test_bootstrap_is_reproducible_and_ohlc_valid(candles_1h):
    a = moving_block_bootstrap_ohlc(candles_1h.tail(300), 32, 24, 4, 91)
    b = moving_block_bootstrap_ohlc(candles_1h.tail(300), 32, 24, 4, 91)
    assert a.shape == (32, 24, 4)
    assert np.array_equal(a, b)
    assert np.all(a[..., 1] >= np.maximum(a[..., 0], a[..., 3]))
    assert np.all(a[..., 2] <= np.minimum(a[..., 0], a[..., 3]))


def test_bootstrap_rejects_too_little_data(candles_1h):
    with pytest.raises(ValueError):
        moving_block_bootstrap_ohlc(candles_1h.head(10), 10, 10, 4)


def test_evaluate_paths_counts_sum():
    paths = np.array([
        [[100, 102, 99, 101], [101, 111, 100, 110]],
        [[100, 101, 94, 95], [95, 96, 92, 94]],
        [[100, 103, 99, 102], [102, 104, 100, 101]],
    ], dtype=float)
    result = evaluate_paths(paths, "LONG", 100, 95, 105, 110, fee_rate=0.0006, slippage_bps=3)
    assert sum(x["count"] for x in result["outcomes"].values()) == 3


def test_tp1_already_realized_before_later_ambiguous_bar_is_preserved():
    from app.simulation.barrier import race_one_path
    import numpy as np

    path = np.array([
        [100, 106, 99, 104, 1, 100],  # TP1 reached first
        [104, 111, 94, 100, 1, 100],  # stop and TP2 overlap; stop chosen for remainder
    ], dtype=float)
    result = race_one_path(path, "LONG", 100, 95, 105, 110, fee_rate=0, slippage_bps=0)
    assert result.outcome == "TP1_FIRST"
    assert result.net_r == 0.0
