# Rebuilt Project Design — v0.4.0

## Product shape

1. Public MEXC Futures market data drives an event-based all-contract ticker watchlist.
2. Completed 3-minute candles are constructed from three contiguous, complete 1-minute candles aligned to the Unix/UTC 3-minute boundary.
3. The strategy engine applies the approved LONG-only RSI/EMA/volume rules, with no live order side effect.
4. Historical tests use the same signal logic, verified contract quantity rules, configurable fee/slippage inputs and conservative liquidation/stop/target precedence.
5. A token-protected web dashboard returns real server-generated chart/backtest files. WhatsApp is support-only while crypto messaging is policy-gated.

## Strategy hypothesis

- RSI(14) >= 70 and rising.
- EMA21 crosses above EMA50 within the recent completed-bar window.
- EMA21 > EMA50 > EMA100.
- Close > EMA21 and > previous completed candle high.
- Volume >= 1.2 times the preceding 20 completed-bar average.
- LONG only. One global simulated position at a time.

These conditions are intentionally explicit and testable; they are not validated as profitable. Do not loosen them to manufacture trades.

## Data and causality

No signal may use an incomplete candle. Signal confirmation occurs at a completed 3-minute candle; any historical simulated entry uses the next candle's open with adverse cost assumptions. The 12H and 3D chart intervals are derived from complete contiguous 4H and daily bars respectively. MEXC does not document a native 3-minute candle interval, so 3-minute bars must be built from 1-minute data.

## Reliability and persistence

- The public client applies bounded timeouts/retries and endpoint-family throttling.
- Missing/empty history is labelled **NOT RUN** instead of being reported as a zero-return backtest.
- Contract metadata must be active and sufficient for quantity sizing before a symbol may be simulated.
- PostgreSQL holds shared service state in Render; an advisory lock prevents two database-connected workers from scanning/executing paper positions simultaneously.
- Unavailable price/funding data must not be silently replaced by fabricated values.

## Safety boundary

Live order execution is disabled and not implemented/released. API credentials do not unlock a real order path. `PAPER_TRADING_ENABLED` is also off by default. The $3 stop-loss does not cap losses at $3; with $1 margin and high leverage, estimated liquidation can happen first. WhatsApp crypto alerts/charts/balance information are not delivered because the relevant Meta policy restricts facilitating virtual-currency transactions.

## Current references

Use `../README.md`, `TECHNICAL_RESEARCH_2026-10-29.md`, `TEST_REPORT.md`, `DEPLOYMENT_V0.4.md`, and `LIVE_TRADING_READINESS.md` as current project records. Older v0.3 strategy notes have been superseded.
