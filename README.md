# PAK TRADING ACADEMY — MEXC SWING ENGINE V11

V11 replaces the V10 breakout → departure → retest → reclaim thesis with a **Trend Pullback / Value Re-entry** strategy while retaining the MEXC data, WhatsApp, futures, risk, and causal backtest infrastructure.

## V11 strategy

`1D TREND → 12H HEALTH → 4H IMPULSE → CONTROLLED PULLBACK / VALUE → 1H LIQUIDITY SWEEP + RECLAIM → NEXT 1H OPEN → STRUCTURAL SL → NEAREST STRUCTURAL TP → POST-COST RR`

Authoritative timeframes are **1D / 12H / 4H / 1H only**. No 5M, 15M, 30M, or hidden lower-timeframe aggregation is used. 12H is causally synthesized from three contiguous completed 4H candles.

### Key V11 principles
- 1D direction is the primary permission gate.
- 12H is a veto/context layer, not an indicator-count gate.
- 4H requires a meaningful impulse (diagnostic threshold: 2.5 ATR) followed by a controlled pullback into value.
- Value combines the 38.2–78.6% retracement area with the 4H EMA21/EMA50 corridor; 50%+ depth is preferred, not a score gate.
- 1H confirms resumption using a **causal liquidity sweep + reclaim**.
- Execution baseline is the **next 1H open** after the completed trigger candle.
- SL is structural invalidation plus an ATR volatility buffer. ATR does not replace invalidation.
- TP is selected from the nearest meaningful confirmed HTF structural magnet **before** RR is evaluated.
- Post-cost RR floor is 1.60R. A farther target is not chosen merely to manufacture RR.
- V11 score is diagnostic only until a large realized trade sample is available.
- Long/short rules are mirrored.

## Backtesting

The existing runner remains causal and uses only completed candles at each 1H decision. Baseline execution is next-1H-open market execution with the simulator's fee/slippage model. Use 1D/7D for smoke testing; use 30D/60D/90D/180D/365D for strategy evaluation and walk-forward validation.

See `V11_STRATEGY_SPEC.md` for the full deterministic specification and `V11_CHANGELOG.md` for the implementation changes.
