# PROJECT STATE — V11

## Current engine
**PAK TRADING ACADEMY — MEXC SWING ENGINE V11**

## Strategy
Trend Pullback / Value Re-entry with 1H Liquidity Sweep + Reclaim.

## Timeframes
**1D / 12H / 4H / 1H only.**

No 5M, 15M, 30M or hidden lower-timeframe aggregation.

## V11 flow
1. 1D macro trend permission
2. 12H health / veto
3. 4H impulse detection
4. 4H controlled pullback into value
5. 1H causal liquidity sweep
6. 1H reclaim
7. Next 1H open execution
8. Structural sweep-based SL + ATR buffer
9. Nearest confirmed HTF structural TP
10. Post-cost RR >= 1.60
11. Signal

## What changed from V10
V10's breakout → mandatory departure → retest → reclaim architecture is no longer the production strategy layer. V10 remains the conceptual control/baseline for future comparisons.

## Research policy
AI-projected win-rate/PF/expectancy ranges are not treated as expected results. V11 must prove itself with 30D/90D+ and then longer walk-forward tests.

## Validation
- Local active test suite: **75 passed, 0 failed, 0 skipped**.
- Python compilation: PASS.
- `app.main` import: PASS.
- `/health`: PASS.
- Supported paper-backtest windows: **1D / 7D / 30D / 60D / 90D / 180D / 365D**.
- Live execution remains disabled until post-fill reconciliation and protective-order verification are implemented.
