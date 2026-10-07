# V11 BALANCED FIXED RELEASE NOTES

## Build
`PAK TRADING ACADEMY — MEXC SWING ENGINE V11 BALANCED FIXED`

## Strategy
Trend Pullback / Value Re-entry with 1H Liquidity Sweep → later Reclaim, with a balanced 1D macro permission model.

## Timeframes
**1D / 12H / 4H / 1H only.** 12H is causally synthesized from three completed 4H candles.

## Execution semantics
Decision = completed 1H close. Paper fill = next 1H open. Live path = MARKET order reference at the next hourly boundary.

## Applied forensic fixes
### V11-balanced over-filtering corrections
- 1D macro permission uses a directional 3-of-5 vote model (price/EMA200, EMA50/EMA200, 3/3 structure, EMA50 slope, ADX) instead of an all-component hard gate.
- 1H wick probes no longer invalidate a 4H impulse; only completed 4H closes through the impulse origin invalidate it.
- 1H reclaim can occur anywhere within the full six-candle window after the sweep.

- Live signal age hard-capped at 5 minutes; stale discoveries are rejected.
- Scanner reprices the trade to the executable next-open quote and revalidates SL/TP geometry and post-cost RR.
- Executor order payload is MEXC Futures MARKET (`type=5`) with no `price` field.
- Sweep and reclaim must occur on separate 1H candles.
- 4H impulses require actual HH/HL or LH/LL continuation structure.
- SHORT impulse indexes and setup-age anchoring are normalized.
- SHORT nearest-target selection is corrected to choose the closest valid downside level.
- Target path is independently checked against confirmed 4H/12H/1D structural obstacles.
- Backtest reports distinguish signal-close RR from actual next-open fill RR.
- Live and paper acceptance share the same deterministic round-trip cost model; funding is informational.
- Obsolete duplicate V10 engine strategy definitions were removed.
- User-facing market/chart timeframe support is restricted to the four V11 frames.
- Paper backtests now support 1D/7D/30D/60D/90D/180D/365D.

## Safety
`SCANNER_ENABLED=false`, `AUTO_SIGNAL_ENABLED=false`, `AUTO_TRADE_ENABLED=false`, `ALLOW_LIVE_EXECUTION=false`, and `LIVE_IMPLEMENTED=false` remain the safe defaults.

## Local verification
- **78 tests passed**
- **0 failed / 0 skipped**
- Python compilation: PASS
- FastAPI import: PASS
- Health check: PASS

This release has not established profitability. Run fresh out-of-sample paper backtests before any future live-execution work.
