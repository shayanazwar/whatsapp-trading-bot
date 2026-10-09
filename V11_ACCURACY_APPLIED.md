# V11 Configuration / Backtest Integrity Fix

## What was wrong

The backtest aggregated each symbol's diagnostics by summing every numeric key. The strategy configuration keys were incorrectly included in those sums. With 200 symbols, the default `2.50 ATR` threshold was reported as `500.00 ATR` and the default 6-bar reclaim window was reported as `1200B` (`2.50 * 200` and `6 * 200`). These were reporting errors; they did not represent the engine's effective settings.

## Fix applied

- Per-symbol event counters are still summed.
- Run-level configuration metadata is excluded from per-symbol aggregation and written exactly once after the run.
- Backtest reports now show the engine build, effective impulse/reclaim/stop configuration, and a stable configuration fingerprint.
- Startup logs include the same fingerprint.
- The run fails rather than returning a report if its engine configuration fingerprint changes mid-run.
- Configuration validation rejects impossible scale values such as `500 ATR` and rejects trigger windows outside 1–24 completed 1H bars.
- Configuration validation also checks the stop-loss mode.

## Effective defaults in this source

- Strategy timeframes: 1D / 12H / 4H / 1H only.
- Minimum 4H impulse: `2.50 ATR`.
- 1H sweep/reclaim trigger window: `6` completed 1H bars.
- Stop-loss mode: `LIQUIDITY_SWEEP`.
- No environment-variable override for these strategy constants is implemented in this source; change them only through reviewed code changes.

## Scope and safety

This patch fixes configuration provenance and backtest diagnostics. It does not tune the trading strategy, claim profitability, or execute live trades. The previously listed `V11_ACCURACY_MODE`, 0.75 ATR extension option, and 2.50R structural-target cap are not independently implemented as runtime controls in this source and must not be assumed active.

## Validation

Run `python -m compileall -q app tests` and `pytest -q` from the project root. A live MEXC historical backtest still requires access to the external MEXC Futures market-data API.
