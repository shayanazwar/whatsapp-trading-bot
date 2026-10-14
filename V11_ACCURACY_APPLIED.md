# V11 Accuracy-First Applied Changes

Applied to the supplied current codebase.

## Frozen architecture
- Only 1D / 12H / 4H / 1H.
- Completed candles only.
- Causal 12H aggregation from contiguous completed 4H candles.
- Breakout + retest / value re-entry framework retained.
- No lower timeframes added.

## Accuracy-first controls now available and enabled by default
- `V11_ACCURACY_MODE=true`
- Structural 4H-origin stop option enabled by default.
- Fresh 1H reclaim: max 3 bars from sweep to reclaim and reclaim must be no more than 2 bars old at entry.
- Maximum entry extension: 0.75 ATR(1H) from swept liquidity level.
- Structural target realism cap: reject structural targets requiring >2.50R before costs.
- Maximum retest depth: 70% of the impulse.
- Breakout quality is measured/tagged but NOT a hard gate by default to avoid unverified filter stacking.

## Implementation integrity
- V11 config is read at decision time, avoiding stale module-import configuration.
- Runtime config fingerprint is included in backtest reports/logs.
- Rejection messages use the actual runtime impulse threshold.
- 500 ATR can now be supplied for plumbing tests without being silently clamped by the engine.

## Trade diagnostics added
Each accepted signal records:
- breakout time
- breakout candle body/range metrics
- retest depth
- reclaim recency
- entry extension
These features can be analyzed against winners/losers later.

## Backtest diagnostics added
- MFE reach rates for 1R / 1.5R / 2R / 2.5R.
- Runtime V11 accuracy configuration in the report.

## Validation
Full test suite: 79 passed.

A live MEXC historical backtest was not executed from this archive because that requires the external MEXC data/API environment.
