# Performance-only patch notes

## Scope

This patch reduces redundant work in repeated hourly engine calls. It does not change strategy rules, thresholds, timeframes, signal admission, stops, targets, portfolio constraints, or the TP counterfactual.

## Changes

- Cached deterministic no-signal diagnostics derived from unchanged completed 4H/12H/1D prefixes.
- Cached no-signal diagnostic 4H impulse/value-zone results on the same explicit runner-owned per-symbol cache.
- Cached the repeated data-quality checks only for an explicitly marked preclosed runner call.
- Added an analyzer-only current-candle sweep/reclaim search that inspects only sweep candles capable of reclaiming within the configured 1H window.
- Reduced repeated trigger searches from every historical value-zone touch to one search from the earliest post-impulse touch.
- Added regression tests for causal preclosed parity, current-trigger parity, historical-trigger behavior, and no-signal diagnostic equality.

## Validation

- `python -m compileall -q app`: PASS
- `pytest -q`: 117 passed
- Existing full V11 fixture output: exact equality between ordinary close-filtered and explicitly preclosed engine calls.
- Existing full-output synthetic sequence digest: identical across all 168 hourly engine decisions before/after the patch.
- Synthetic microbenchmark: 168 engine calls over fixed 1D/12H/4H context with a growing 1H prefix completed in 2.533 s before and 0.204 s after (~12.4x faster in that test). This is not an end-to-end MEXC benchmark or a guarantee of production speedup.

## Deliberately not changed

Simulator stop-gap fills, realized funding costs, data-coverage reporting, fixed-period reporting semantics, all strategy thresholds, trade selection, and portfolio reservations are unchanged. Simulator realism changes must be benchmarked and reported separately because they can change backtest outcomes.
