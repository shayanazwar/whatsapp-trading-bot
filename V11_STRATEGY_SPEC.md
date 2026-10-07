# MEXC SWING ENGINE V11-balanced — STRATEGY SPECIFICATION

## 1. Purpose

V11 is an independent strategy-layer rebuild based on the common useful conclusions from the Claude, GPT, and Gemini audits. The V10 infrastructure is retained where it is useful, but the V10 BOS → mandatory departure → retest → reclaim thesis is removed.

The three analyses converge on Trend Pullback / Value Re-entry as the primary candidate, with liquidity-sweep/absorption concepts added as the 1H confirmation layer. Their numerical accuracy/expectancy projections are treated as hypotheses, not expected results.

## 2. Timeframes

Only:
- 1D — macro directional permission
- 12H — intermediate trend health / correction context
- 4H — impulse, pullback, value and structural invalidation
- 1H — causal trigger and execution signal

No 5M, 15M, 30M, or hidden lower-timeframe aggregation.

12H is built from exactly three contiguous completed 4H candles.

## 3. Causality

- Decisions occur only at completed 1H candle close.
- Only candles closed by the decision timestamp are passed to the engine.
- 4H/12H pivots use 3-left/3-right confirmation and therefore exist only after their right-side candles close.
- The 1H trigger itself uses no future pivot information.
- Baseline paper execution occurs at the next 1H candle open with modeled slippage.

## 4. State machine

`DATA → 1D_REGIME → 12H_HEALTH → 4H_IMPULSE → VALUE_TEST → 1H_SWEEP → 1H_RECLAIM → TRIGGERED → NEXT_1H_OPEN → ENTERED → TP/SL/TIME_STOP`

Multiple candidate 4H impulses may remain eligible. The engine does not rely on V10's latest-only BOS selection.

## 5. 1D regime
V11-balanced uses a directional 3-of-5 macro vote rather than an all-components
hard gate. This restores the more practical V10 macro permission model without
restoring V10's late BOS/departure/retest entry state machine.

LONG permission requires at least 3 bullish votes:
- close > EMA200
- EMA50 > EMA200
- confirmed 3/3 swing structure = HH/HL
- EMA50 slope > 0
- ADX >= 14

SHORT permission requires at least 3 bearish votes:
- close < EMA200
- EMA50 < EMA200
- confirmed 3/3 swing structure = LH/LL
- EMA50 slope < 0
- ADX >= 14

The selected side must have more macro votes than the opposite side. Exact HH/HL
or LH/LL structure remains mandatory for the 4H trend-continuation impulse.

## 6. 12H context

12H is intentionally a light veto layer.

LONG is blocked only when the 12H structure is bearish, price is below EMA50, and EMA50 slope is negative.

SHORT is blocked only when the 12H structure is bullish, price is above EMA50, and EMA50 slope is positive.

Otherwise the context is HEALTHY or NEUTRAL.

## 7. 4H impulse

A completed impulse must:
- use confirmed 3/3 pivots;
- produce a new structural extreme in the trend direction;
- be at least 2.5 ATR(4H);
- remain within the active setup window of 30 completed 4H bars;
- remain structurally intact.

LONG: confirmed swing low → higher swing high.
SHORT: confirmed swing high → lower swing low.

The impulse origin is the primary structural invalidation anchor.

## 8. Value zone

The value zone is the intersection of:
- 38.2–78.6% retracement of the latest impulse; and
- the 4H EMA21/EMA50 corridor, expanded by 0.15 ATR for ordinary noise.

If the corridor and retracement band do not overlap, the retracement band remains the fallback value zone. This prevents unnecessary zero-signal behavior.

50%+ retracement is recorded as preferred/deep value evidence, not a hard gate.

## 9. Pullback validity

LONG:
- price enters value after the impulse high;
- no completed 4H close below the impulse origin;
- 1H wick probes of the origin are allowed; only completed 4H closes invalidate the impulse.

SHORT is the exact inverse.

## 10. 1H liquidity sweep + reclaim

The trigger is causal and does not use centered pivots.

For LONG:
1. After value is touched, a 1H candle sweeps below the lowest low of the previous three completed 1H candles.
2. A subsequent 1H candle closes back above that swept level and closes bullish.
3. The reclaim must occur within six 1H bars.

For SHORT:
1. Sweep above the highest high of the previous three completed 1H candles.
2. Subsequent candle closes back below the swept level and closes bearish.
3. Reclaim within six 1H bars.

RVOL ≥ 1.20, body quality and close-location are supporting evidence, not independent hard gates. This directly addresses the audits' warning about stacked correlated gates.

## 11. Entry

Signal is generated at the completed reclaim candle close.

Baseline execution:
- market entry at the next 1H open;
- adverse slippage is modeled in the backtester.

No historical limit-fill assumption is used for the baseline strategy.

## 12. Stop loss

Structural invalidation comes first.

LONG:
`liquidity-sweep low - volatility buffer`

SHORT:
`liquidity-sweep high + volatility buffer`

Buffer:
`max(0.20 × ATR4H, 0.15 × ATR1H)`

The 4H impulse origin remains a higher-level structural veto, but the trade stop is anchored to the immediate liquidity-sweep invalidation. The ATR bounds in the validator are only safety sanity limits (0.20–3.00 ATR4H). The strategy does not compress or widen a valid structural stop to manufacture a target RR.

## 13. Target

Target selection is structural-first.

Candidate levels come from confirmed 4H, 12H and 1D swing structures plus the impulse extreme.

The **nearest meaningful structural target beyond entry is selected first**.

Only after that selection is RR calculated. If the nearest structural target does not provide sufficient post-cost RR, the setup is rejected. The engine does not jump to a farther level merely to satisfy RR.

## 14. RR and costs

Post-cost RR floor: **1.60R**.

The V11 code estimates round-trip cost from configuration and applies that cost before accepting the setup.

The 1.60R number is an engineering floor, not a claim of profitability.

## 15. Shock veto

A 1H range greater than 4.5 ATR(1H) blocks the setup. This is a safety veto against abnormal execution conditions.

## 16. Scoring

V11 score is diagnostic only. It contains:
- impulse structure
- value location
- liquidity reclaim
- target geometry
- trend context
- volatility

There is **no score threshold** in V11 until a sufficiently large realized sample exists to calibrate score buckets against outcomes.

## 17. Long/short symmetry

Every directional rule has an explicit mathematical inverse. No short-only or long-only special-case trigger logic is part of V11.

## 18. Backtest interpretation

- 1D and 7D = smoke tests only.
- 30D = statistical sanity check.
- 90D+ = first meaningful comparison.
- 180–365D walk-forward is required before production confidence.
- TP-before-SL defines a win for the single-target simulator.
- Same-candle ambiguity follows the existing simulator's SL-first rule.
- Open trades at the end of a test must not be silently counted as wins/losses.

Recommended acceptance research criteria from the audits are treated as evaluation targets, not guarantees: seek positive post-cost expectancy, PF > 1.25, sufficient trade count, side symmetry, symbol concentration control, and stability under parameter perturbation.
