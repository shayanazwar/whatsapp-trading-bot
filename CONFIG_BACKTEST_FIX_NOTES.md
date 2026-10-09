# V11 Config / Backtest Fix Notes

Build: `V11-balanced-value-pullback-liquidity-reclaim-config-guard-1`

## Root cause
The per-symbol diagnostics merge added strategy configuration values as if they were event counts. With the 200-symbol cap, this inflated the default impulse threshold from `2.50 ATR` to a displayed `500.00 ATR`, and the reclaim window from `6` to a displayed `1200B`. This caused a mismatch between the report header and the engine rejection messages.

## Changes
1. Configuration metadata is no longer summed during symbol aggregation; the runner records each configuration value once per run.
2. The report and start log now include the engine build and configuration fingerprint.
3. The report shows the effective impulse threshold, 1H reclaim window, stop mode, and short-range mode on every report.
4. Engine config validation explicitly rejects scaled values such as 500 ATR / 1200 bars and invalid stop modes.
5. The report no longer prints malformed units such as `N/AR` for metrics that have no data.
6. Corrected `V11_ACCURACY_APPLIED.md` so it does not claim runtime controls that are absent from the source.
7. Added regression tests for 200-symbol config aggregation, validation and report identity.

## Expected report config
- `Impulse 2.50 ATR`
- `Reclaim 6 x 1H`
- `SL LIQUIDITY_SWEEP`
- A 12-character configuration fingerprint

## Validation results
- `python -m compileall -q app tests`: PASS
- `pytest -q`: **88 passed**
- Empty-universe backtest/report smoke test: PASS

No live MEXC backtest was run from this environment; this package does not execute live orders as part of validation.
