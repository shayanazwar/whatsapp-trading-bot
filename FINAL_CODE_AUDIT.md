# V11 Fixed Release Audit

This release applies the forensic fixes to the MEXC Swing Engine V11 architecture.

## Strategy contract
- Authoritative timeframes: **1D / 12H / 4H / 1H only**.
- 12H is causally synthesized from three contiguous completed 4H candles.
- Decision: completed 1H close. Live entry reference: next 1H open using a MARKET order.
- V11 setup: trend pullback / value re-entry with 1H liquidity sweep followed by a later 1H reclaim candle.
- 4H structure requires real HH/HL for LONG or LH/LL for SHORT.
- Structural target is selected first; target-path clearance is independently verified before RR acceptance.

## Fixed critical issues
1. Live scanner no longer accepts a 90-minute-old signal as if it were the next 1H open. Freshness is capped at 5 minutes and stale discoveries are rejected.
2. Live order construction now builds a MEXC Futures MARKET payload (`type=5`) with no limit price.
3. Sweep/reclaim now requires separate sweep and reclaim candles.
4. SHORT impulse indexing and setup-age anchoring are normalized and regression-tested.
5. SHORT target selection uses the nearest downside structural level instead of the farthest one.
6. Target-path status is derived from actual completed HTF levels rather than being asserted.
7. Backtest reports now distinguish signal-close RR from actual next-open fill RR.
8. Live validation uses the same deterministic round-trip cost model as the paper simulator; funding remains informational.
9. Legacy duplicate V10 engine definitions were removed from `app/analysis/engine.py`.
10. User-facing market/chart timeframe aliases are limited to the four V11 timeframes.

## Safety
- `LIVE_IMPLEMENTED = False` remains enforced.
- Default scanner/signal/trade/live-execution switches remain **OFF**.

## Validation
- Python compilation: PASS
- Pytest suite: PASS (75 tests)
- FastAPI import: PASS
- Health route: PASS

This is a code-integrity/release audit, not a guarantee of trading profitability. Full walk-forward and live-paper validation should still be performed before enabling any real execution path.
