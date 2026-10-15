# V11 Research-Backed Accuracy / Diagnostics Patch Manifest

This is a minimal replacement package for the supplied `whatsapp-trading-bot-main` ZIP. Replace files at the same relative paths from the project root.

## Runtime files

- `app/analysis/engine.py`
  - Fixes sweep/reclaim window semantics: the bar limit is measured from the actual liquidity sweep, after the first active value-zone touch; a current completed-candle reclaim is prioritized over stale historic triggers.
  - Treats ADX as trend-strength context, not a directional vote; daily direction is based on four directional inputs.
  - Adds entry/pullback quality measurements and opt-in experimental filters for reclaim delay, entry extension, and retracement depth. The filter mode defaults OFF and does not alter the established baseline unless explicitly enabled.
  - Does not change the existing stop placement, structural target selection, RR floor, or approved timeframes.
- `app/backtest/simulator.py`
  - Persists the new signal quality measurements to each simulated trade and measures the actual fill's extension from the swept level when enough data is available.
- `app/backtest/runner.py`
  - Emits accuracy-mode config diagnostics so backtest output tells the user whether experimental filters were enabled and with what settings.
- `app/backtest/report.py`
  - Reports TP-vs-SL entry-quality averages and sample counts; clearly labels these as diagnostic, not proof of prediction.

## Regression tests

- `tests/test_v11_strategy.py`: adds coverage for sweep-relative reclaim windows, default-off experimental settings, extension/depth filters when explicitly enabled, correct ADX vote semantics, and decision-time quality metadata.
- `tests/test_backtest_feature.py`: checks TP/SL outcome quality comparisons and actual-fill extension measurement.

## Documentation

- `V11_ACCURACY_RESEARCH.md`: research rationale, limits, and controlled-test protocol.
- `V11_ACCURACY_APPLIED.md`, `V11_STRATEGY_SPEC.md`, `V11_CHANGELOG.md`, `V11_RELEASE_NOTES.md`, `PROJECT_STATE.md`, and `README.md`: align the project's documentation with the actual implementation and mark filters as experimental/off by default.
- `TEST_RESULTS.md`: validation actually performed.

## Intentionally not changed

No lower timeframes were introduced. The approved frames remain exactly 1D / 12H / 4H / 1H. The patch does not change stop-loss geometry, target-selection logic, position sizing, portfolio risk limits, fees/slippage assumptions, API keys, database files, deployment configuration, or live-execution defaults. No claim is made that the change improves realized profitability; historical MEXC candles and a frozen universe were not included, so a corrected performance backtest was not reproducible here.
