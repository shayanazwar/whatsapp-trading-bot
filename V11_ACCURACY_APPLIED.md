# V11 Accuracy Research Controls — Implemented Status

The controls below are available in code but are **disabled by default** because no controlled historical test yet proves they increase expectancy. Do not treat these thresholds as a proven edge.

## Preserved baseline
- Only 1D / 12H / 4H / 1H analysis; completed candles only.
- 12H synthesized from exactly three contiguous completed 4H bars.
- Existing structural target selection, liquidity-sweep stop, costs, and portfolio controls unchanged.
- Live trading and order execution defaults remain disabled.

## Correctness fixes
- ADX is trend strength and is no longer counted as a directional vote. Daily permission uses three of four directional observations.
- Liquidity sweeps may occur any time after value is touched while the 4H setup is active. The reclaim window is measured from each actual sweep and an eligible current-candle reclaim takes priority over stale historical triggers.
- The previously misleading `setup_pullback_touch_time` now records the first value-zone touch associated with the selected trigger, not the sweep time.
- Signal metadata records impulse age, pullback depth, sweep-to-reclaim bars, reclaim body ratio, close-location, relative volume, and entry extension in ATR.
- Backtest report compares measured quality averages for TP and SL samples. These are diagnostics, not causal claims or acceptance gates.

## Optional accuracy experiment
Set `V11_ACCURACY_MODE=true` in an isolated paper/research deployment to activate additional rejection rules:
- `V11_ACCURACY_MAX_RECLAIM_BARS=3` (allowed 1–6; max bars from actual sweep to reclaim)
- `V11_ACCURACY_MAX_ENTRY_EXTENSION_ATR=0.75` (allowed >0–5 ATR)
- `V11_ACCURACY_MAX_RETEST_DEPTH=0.70` (allowed >0–1.0 of the impulse leg)

Defaults are false / 3 / 0.75 / 0.70. The filters are not proven profitable. Run baseline and experiment on the exact same frozen candles, universe, period, and execution assumptions. Do not enable in a live deployment based solely on this patch.

## Research rationale
The controls convert common discretionary concepts—fresh reclaim, avoiding extended entries, limiting very deep retracements—into explicit testable variables. Academic work on backtest overfitting and cryptocurrency momentum risk supports out-of-sample validation and volatility/risk diagnostics, not assuming that any fixed threshold improves returns. See `V11_ACCURACY_RESEARCH.md`.
