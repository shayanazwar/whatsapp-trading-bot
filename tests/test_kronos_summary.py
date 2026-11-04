import numpy as np
import pytest

from app.forecasting.kronos import summarize_forecast_paths


def test_forecast_summary_uses_real_path_distribution_and_median_ohlc():
    paths = np.array([
        [[100, 103, 99, 102, 10, 1000], [102, 105, 101, 104, 11, 1100]],
        [[100, 102, 98, 99, 9, 900], [99, 101, 97, 98, 8, 800]],
        [[100, 104, 99, 103, 12, 1200], [103, 107, 102, 106, 13, 1300]],
    ], dtype=float)
    result = summarize_forecast_paths(paths, np.linspace(95, 100, 100))
    assert result["samples"] == 3
    assert result["horizon"] == 2
    assert result["expected_close"] == pytest.approx(104.0)
    assert result["forecast_direction"] == "BULLISH"
    assert result["bullish_path_share"] == pytest.approx(2 / 3)
    assert result["bearish_path_share"] == pytest.approx(1 / 3)
    assert len(result["median_ohlc_by_step"]) == 2
    for candle in result["median_ohlc_by_step"]:
        op, high, low, close = candle
        assert high >= max(op, close)
        assert low <= min(op, close)
    assert result["calibration_status"] == "UNVALIDATED_PATH_SHARE_NOT_MARKET_PROBABILITY"
    assert result["uncertainty_width_pct"] > 0


def test_forecast_summary_rejects_invalid_path_inputs():
    with pytest.raises(ValueError, match="shape"):
        summarize_forecast_paths(np.ones((3, 4)), [100, 101])
    with pytest.raises(ValueError, match="historical closes"):
        summarize_forecast_paths(np.ones((3, 2, 4)), [100])
