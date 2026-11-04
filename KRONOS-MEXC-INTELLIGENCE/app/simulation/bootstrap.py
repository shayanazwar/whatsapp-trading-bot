from __future__ import annotations

import numpy as np
import pandas as pd

from app.market.candles import validate_candles, validate_candle_continuity


def moving_block_bootstrap_ohlc(candles: pd.DataFrame, n_paths: int = 10_000,
                                horizon: int = 24, block_length: int = 4,
                                seed: int = 42, condition_on_volatility: bool = True) -> np.ndarray:
    """Resample contiguous normalized OHLC candle blocks, not independent bars.

    Output shape is [paths, horizon, 4] in absolute OHLC prices. It is a historical
    scenario generator, not a predictive probability model.
    """
    raw = validate_candles(candles)
    diffs = raw["timestamp"].astype("int64").diff().dropna() / 1_000_000_000
    if diffs.empty:
        raise ValueError("Insufficient historical candle timestamps")
    timeframe_seconds = int(diffs.mode().iloc[0])
    frame = validate_candle_continuity(candles, timeframe_seconds)
    if n_paths < 1 or horizon < 1 or block_length < 1:
        raise ValueError("Simulation dimensions must be positive")
    if len(frame) < max(40, block_length + 20):
        raise ValueError("Insufficient historical candles for sequence bootstrap")
    prev_close = frame["close"].shift(1).to_numpy(dtype=float)
    bars = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    ratios = bars[1:] / prev_close[1:, None]
    if not np.isfinite(ratios).all():
        raise ValueError("Invalid normalized historical candle returns")
    log_returns = np.diff(np.log(frame["close"].to_numpy(dtype=float)))
    return_series = pd.Series(log_returns)
    rolling_vol = return_series.rolling(20, min_periods=15).std().to_numpy()
    rolling_drift = return_series.rolling(20, min_periods=15).mean().to_numpy()
    latest_vol = float(rolling_vol[-1]) if len(rolling_vol) and np.isfinite(rolling_vol[-1]) else np.nan
    latest_drift = float(rolling_drift[-1]) if len(rolling_drift) and np.isfinite(rolling_drift[-1]) else np.nan
    valid_starts = np.arange(0, len(ratios) - block_length + 1)
    # Regime-match candidate contiguous blocks on volatility AND drift. This
    # reduces mismatched trending/flat regimes without claiming calibrated odds.
    if condition_on_volatility and np.isfinite(latest_vol) and np.isfinite(latest_drift):
        start_vol_values, start_drift_values = [], []
        for i in valid_starts:
            lo = max(0, i)
            hi = min(len(rolling_vol), i + block_length)
            vol_window = rolling_vol[lo:hi]
            drift_window = rolling_drift[lo:hi]
            finite_vol = vol_window[np.isfinite(vol_window)]
            finite_drift = drift_window[np.isfinite(drift_window)]
            start_vol_values.append(float(finite_vol.mean()) if len(finite_vol) else np.nan)
            start_drift_values.append(float(finite_drift.mean()) if len(finite_drift) else np.nan)
        start_vol = np.asarray(start_vol_values, dtype=float)
        start_drift = np.asarray(start_drift_values, dtype=float)
        finite = np.isfinite(start_vol) & np.isfinite(start_drift)
        if finite.sum() >= max(10, block_length):
            vol_scale = max(float(np.nanstd(start_vol[finite])), 1e-8)
            drift_scale = max(float(np.nanstd(start_drift[finite])), 1e-8)
            distances = np.sqrt(
                ((start_vol[finite] - latest_vol) / vol_scale) ** 2
                + ((start_drift[finite] - latest_drift) / drift_scale) ** 2
            )
            cutoff = np.quantile(distances, 0.60)
            matched = valid_starts[finite][distances <= cutoff]
            if len(matched) >= max(10, block_length):
                valid_starts = matched
    if not len(valid_starts):
        raise ValueError("No valid historical blocks are available")
    rng = np.random.default_rng(seed)
    output = np.empty((n_paths, horizon, 4), dtype=np.float32)
    blocks_needed = (horizon + block_length - 1) // block_length
    base_price = float(frame.iloc[-1]["close"])
    for p in range(n_paths):
        sampled = []
        for _ in range(blocks_needed):
            start = int(rng.choice(valid_starts))
            sampled.extend(ratios[start:start + block_length])
        price = base_price
        for t, (ro, rh, rl, rc) in enumerate(sampled[:horizon]):
            op, hi, lo, cl = price * ro, price * rh, price * rl, price * rc
            hi = max(hi, op, cl)
            lo = min(lo, op, cl)
            output[p, t] = (op, hi, lo, cl)
            price = cl
    return output
