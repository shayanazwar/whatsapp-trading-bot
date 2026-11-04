import numpy as np

from app.forecasting.evaluation import evaluate_forecast_paths


def test_forecast_evaluation_compares_kronos_to_naive_and_drift_baselines():
    paths = np.array([
        [[101, 103, 100, 102, 1, 1], [103, 106, 102, 105, 1, 1]],
        [[100, 102, 99, 101, 1, 1], [102, 105, 101, 104, 1, 1]],
    ], dtype=float)
    actual = np.array([102.0, 104.5])
    result = evaluate_forecast_paths(paths, actual, 100.0, drift_mean_log_return_per_bar=0.01)

    baseline = result["baseline_comparison"]
    assert baseline["naive_last_value"]["mae_close"] > 0
    assert baseline["drift"]["mae_close"] >= 0
    assert isinstance(baseline["kronos_mae_improvement_vs_naive_pct"], float)
    assert result["directional_accuracy_final"] == 1.0


def test_forecast_evaluation_without_drift_uses_naive_fallback():
    paths = np.array([
        [[100, 102, 99, 101, 1, 1], [100, 102, 99, 100, 1, 1]],
        [[100, 102, 99, 100, 1, 1], [100, 102, 99, 101, 1, 1]],
    ], dtype=float)
    result = evaluate_forecast_paths(paths, np.array([100.5, 100.8]), 100.0)
    comparison = result["baseline_comparison"]
    assert comparison["drift_mean_log_return_per_bar"] is None
    assert comparison["drift"] == comparison["naive_last_value"]
