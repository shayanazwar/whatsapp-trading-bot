from __future__ import annotations

import numpy as np


def evaluate_forecast_paths(paths: np.ndarray, actual_close: np.ndarray, baseline_close: float) -> dict:
    """Evaluate an ensemble against realized closes; all scores are out-of-sample only."""
    samples = np.asarray(paths, dtype=float)
    actual = np.asarray(actual_close, dtype=float)
    if samples.ndim != 3 or samples.shape[-1] < 4:
        raise ValueError("paths must have shape [samples, horizon, OHLCVA]")
    if actual.ndim != 1 or len(actual) != samples.shape[1]:
        raise ValueError("actual_close length must match forecast horizon")
    if len(samples) < 2 or not np.isfinite(samples).all() or not np.isfinite(actual).all():
        raise ValueError("Forecast and actual data must be finite with at least two paths")
    if baseline_close <= 0 or (actual <= 0).any():
        raise ValueError("Prices must be positive")
    predicted = samples[:, :, 3]
    median = np.median(predicted, axis=0)
    p10, p90 = np.quantile(predicted, [0.10, 0.90], axis=0)
    errors = median - actual
    coverage = (actual >= p10) & (actual <= p90)
    # Empirical ensemble CRPS; sorted formula avoids a samples x samples matrix.
    n = predicted.shape[0]
    crps_values = []
    weights = (2 * np.arange(1, n + 1) - n - 1).astype(float)
    for step in range(predicted.shape[1]):
        ordered = np.sort(predicted[:, step])
        first = np.mean(np.abs(ordered - actual[step]))
        half_pairwise = np.sum(weights * ordered) / (n * n)
        crps_values.append(float(first - half_pairwise))
    final_direction = float((median[-1] > baseline_close) == (actual[-1] > baseline_close))
    return {
        "samples": int(samples.shape[0]),
        "horizon": int(samples.shape[1]),
        "mae_close": float(np.mean(np.abs(errors))),
        "rmse_close": float(np.sqrt(np.mean(errors ** 2))),
        "directional_accuracy_final": final_direction,
        "directional_accuracy_by_step": float(np.mean((median > baseline_close) == (actual > baseline_close))),
        "p10_p90_coverage": float(np.mean(coverage)),
        "mean_p10_p90_width": float(np.mean(p90 - p10)),
        "crps_ensemble_mean": float(np.mean(crps_values)),
        "median_close_forecast": median.tolist(),
        "actual_close": actual.tolist(),
        "p10_close": p10.tolist(),
        "p90_close": p90.tolist(),
        "calibration_state": "SINGLE_FORECAST_EVALUATION_NOT_GLOBAL_CALIBRATION",
    }
