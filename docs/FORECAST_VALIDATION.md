# Forecast validation

For every forecast origin, store the model/tokenizer revisions, lookback end timestamp, horizon, sample seed and all generated quantiles. Once future candles close, join only the corresponding realized horizon and calculate MAE, RMSE, directional accuracy, quantile coverage, interval width and CRPS where sample data supports it.

Use chronological walk-forward evaluation with untouched test periods. Compare Kronos against a no-change/random-walk baseline, a historical-return baseline, an ATR scenario baseline and a regime-conditioned bootstrap baseline. Report by symbol, timeframe and horizon. Do not tune on the final test window.

`bullish_path_share` and `bearish_path_share` describe the share of generated model paths ending above/below the last close; they are not calibrated market probabilities. Calibration labels must stay `UNVALIDATED` until the out-of-sample reliability, sample size and uncertainty have been reviewed.
