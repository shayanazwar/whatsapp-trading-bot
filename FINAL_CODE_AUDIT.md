# FINAL CODE AUDIT — 2026-09-25

Audited against the uploaded complete project ZIP and the Render logs supplied in this conversation.

## Runtime/code fixes

- MEXC Futures is the sole market path.
- `closed_candle_rows()` accepts timeframe names or integer milliseconds and normalizes candle timestamps.
- Scanner no longer assumes normalized candles are positional lists.
- 4H / 1H / 15M / 5M hierarchy is enforced; 1D context is used for structural targets.
- BOS retest search is strictly post-BOS and setup freshness is bounded.
- 5M trigger requires a directional candle body, breakout, RSI, RVOL and body/range quality.
- TP1 and TP2 must be two distinct confirmed structural/liquidity levels; fixed synthetic 1.2R/2R targets are not manufactured.
- Technical analysis no longer rejects merely because live futures context is still pending.
- BTC directional/shock filtering is applied before final live-context checks.
- MEXC ticker fields are reused for bid/ask/index/fair/funding when available.
- MEXC recent trades parse the official `T` direction and `v` volume fields.
- Funding presence is not treated as a directional confirmation by itself.
- Scanner records rejection stages and technical gate failures.
- Ticker freshness now fails closed when the exchange timestamp is missing or stale.
- MEXC public calls use a shared conservative pacing/window limiter with rate-limit detection, bounded exponential backoff, `Retry-After` support, jitter, and a shared cooldown across concurrent tasks.
- Futures risk sizing uses documented USDT `equity` as total Futures equity; legacy `0.01` configuration is normalized to `1.0%` and the hard auto-trade cap remains 1%.
- Signal cooldown/re-entry logic is retained and signal statistics only count an actual successful WhatsApp delivery as `sent`.
- Scheduler is driven by new 5-minute candle boundaries and prevents overlapping scans.
- Shared MEXC HTTP client is closed cleanly on application shutdown.
- Manual analysis/signal output uses Score `/100` and Families `/6` instead of legacy confluence-only output.

## Safety state

Live trading is still hard disabled:

```text
AUTO_TRADE_ENABLED=false
ALLOW_LIVE_EXECUTION=false
LIVE_IMPLEMENTED=false
```

Do not enable live execution from this release. The execution path still requires historical backtesting, paper/forward testing, fill reconciliation, protective SL/TP verification and emergency recovery before any real order placement.

## Verification performed

```text
pytest -q
17 passed

python -m compileall -q app tests
OK
```

The supplied Render scan log showed the previous positional-candle crash had been removed; the service reached a clean completed scan with 120 symbols and zero scanner errors, but zero valid signals. fileciteturn59file0L290-L300

The current release adds explicit technical rejection diagnostics so the next Render cycle can show which gate is stopping candidates instead of only reporting `NO TRADE`.


## Finalized backtest performance audit — 2026-09-28

The supplied Render logs showed the backtest remaining at `0/300` for several minutes and reaching only `2/300` after approximately 769 seconds. The source audit identified several concrete causes in the backtest path:

1. `MAX_SYMBOL_CONCURRENCY` was hard-capped at `1` even though the application constructed the runner with `max_concurrency=4`.
2. `_closed_slice()` used `bisect_right(...)+1`, which could include the next candle and create historical look-ahead.
3. The 15M prefilter generated large overlapping candidate sets and rescanned candle history for each retest.
4. The full engine was being invoked for candidates that could already be rejected by the 4H/1H hard direction gates.
5. The full engine's 15M entry-confirmation requirement was available as an exact semantics-preserving prefilter but was not used before expensive target/score calculations.
6. `analyze_candles()` recomputed identical BOS event sets multiple times per analysis.

The finalized release addresses all six issues without lowering the signal threshold, RR requirement, or higher-timeframe gates.

The `4_USDT` symbol observed in Render is a legitimate MEXC Futures perpetual contract, so no symbol-format blacklist was added.


## MEXC public API rate-limit audit — 2026-09-28

The supplied Render log contained a concrete MEXC Futures public API error: `Requests are too frequent, please try again later` while fetching BTC_USDT klines. The surrounding requests were otherwise returning HTTP 200, so this is a rate-management issue rather than an API credential failure.

Replacement changes:

- `MexcClient` now applies a shared public-request pacing gate before every public REST request.
- Default pacing is 0.20 seconds between request starts plus an 8-request/2-second rolling window.
- MEXC rate-limit responses are detected from HTTP status and JSON message/code forms.
- Retries are bounded and use exponential backoff, `Retry-After` when supplied, and jitter.
- A shared pause prevents concurrent scanner tasks from retrying simultaneously.
- Scanner concurrency is hard-capped at 4 to prevent excessive task fan-out even when an older environment variable requests more.
- Trading strategy thresholds, setup definitions, score requirements, RR requirements, and live-trading safety flags are unchanged.

Verification:

```text
pytest -q -> 37 passed
python -m compileall -q app tests -> OK
```
