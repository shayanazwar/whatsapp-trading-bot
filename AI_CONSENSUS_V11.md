# AI CONSENSUS USED FOR V11

This file records the useful strategy conclusions extracted from the supplied Claude, GPT, and Gemini analyses. It intentionally does not copy their projected performance numbers as facts.

## Common conclusions

- V10's core problem is architectural, not simply a threshold problem.
- Breakout → mandatory departure → retest → reclaim creates late/adverse entries.
- Target selection should be structural first and RR second.
- Stop loss should represent actual invalidation; ATR should act as a volatility buffer.
- Correlated indicator stacking and an uncalibrated score should not be treated as accuracy.
- Long/short rules should be mathematically symmetric.
- Causal processing must remain strict: only completed 1D/12H/4H/1H candles.
- Baseline backtests should execute at the next 1H open rather than pretending the signal close is executable.
- V10 should remain a control/baseline for later comparisons.

## Claude-derived design points

- Primary candidate: Trend Pullback to Value with 1H Resumption.
- 4H impulse, controlled pullback, value zone, 1H resumption.
- Use structural invalidation and nearby structural target.
- Do not use a score threshold until enough realized trades exist to calibrate it.
- 1D/7D are smoke tests; longer windows are required for decisions.

## GPT-derived design points

- Primary candidate: Trend Pullback — Value Re-entry Continuation.
- 1D trend → 12H health → 4H impulse → pullback/value → 1H causal trigger → next 1H open.
- Value combines retracement geometry and EMA21/EMA50 context.
- 1H trigger should avoid future-dependent centered pivots.
- Structural stop first; ATR is only a buffer.
- Nearest valid structural target first; reject if it cannot support acceptable post-cost expectancy/RR rather than manufacturing a farther target.

## Gemini-derived design points

- Primary candidate: HTF Value Pullback & Structural Absorption.
- Add liquidity-sweep/reclaim behavior as the 1H confirmation concept.
- Deep discount/premium is useful as supporting context.
- RVOL and absorption are supporting evidence, not a license to create a large hard-gate stack.

## Deliberately not copied as facts

The AI reports contained numerical projections for win rate, PF, expectancy, trade frequency, and drawdown. V11 does not assume any of those numbers. They are hypotheses to be tested out-of-sample.
