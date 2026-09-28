# Project State — 2026-09-25

## Current release

This package is the corrected deterministic MEXC Futures scanner release for the Pak Trading Academy WhatsApp bot.

## Canonical runtime

```text
Docker -> uvicorn app.main:app
```

The `app/` package is canonical.

## Deterministic decision flow

```text
MEXC Futures data
  -> data integrity / closed candles
  -> 120-symbol universe
  -> BTC market filter
  -> 1D context
  -> 4H regime
  -> 1H direction + protected structure
  -> 15M BOS + post-BOS retest
  -> 5M trigger
  -> momentum / volume / volatility
  -> structural target path
  -> structural SL / RR
  -> hard technical gates
  -> MEXC executable quote / spread / index-fair / funding / depth / trade flow
  -> grouped 100-point score
  -> final validator (score >=82, RR >=2, families >=5/6)
  -> symbol/side cooldown
  -> WhatsApp signal
```

## Safety state

```text
SCANNER_ENABLED=false
AUTO_SIGNAL_ENABLED=false
AUTO_TRADE_ENABLED=false
ALLOW_LIVE_EXECUTION=false
```

These are the package defaults.

## Important limitations

The scanner is deterministic, but a target win rate such as 70–80% is not guaranteed. The score is an evidence score, not a probability.

Live execution is deliberately disabled. Missing live-trading components still include post-fill reconciliation, protective-order verification, partial TP/break-even management, portfolio exposure controls and emergency recovery.

## Finalized backtest/performance fixes — 2026-09-28

The backtest path was audited against Render behavior showing only 2/300 symbols after ~769 seconds. The root causes were confirmed in `app/backtest/runner.py` and `app/analysis/engine.py`.

Implemented fixes:

- Backtest symbol concurrency is now effectively 4 instead of being hard-capped at 1.
- Historical backtest slices no longer include the next/open candle; the previous `+1` slice introduced a look-ahead candle.
- Historical BTC/symbol histories are explicitly trimmed to closed candles at the backtest end boundary.
- 15M BOS/retest candidate-window generation now uses indexed/binary-search ranges instead of repeatedly scanning the complete candle history.
- Candidate metadata preserves LONG and SHORT candidates independently when both occur at the same timestamp.
- A cached 4H/1H higher-timeframe prefilter rejects candidates that the authoritative engine must reject anyway.
- The exact authoritative 15M entry-confirmation gate is used as a semantics-preserving prefilter before expensive full-engine analysis.
- Repeated BOS calculations inside `analyze_candles()` were reduced by computing each side's BOS event set once and reusing it for selection and diagnostics.
- Backtest timing/progress instrumentation records data and analysis duration per symbol.
- `4_USDT` is not treated as malformed: it is a real MEXC USDT perpetual contract and remains eligible subject to the normal contract filters.

Live trading remains disabled. No strategy thresholds were weakened to manufacture signals.

## Verification

```text
pytest -q
37 passed
python -m compileall -q app tests
OK
```


## Finalized MEXC public-request rate-limit protection — 2026-09-28

The Render scanner log exposed a real MEXC public API rate-limit response (`Requests are too frequent`) during concurrent symbol scanning. The trading rules were left unchanged.

Implemented protection:

- A single shared limiter now paces every public MEXC REST request across concurrent scanner/backtest tasks.
- Default pacing is conservative: at least 0.20 seconds between public request starts and no more than 8 requests in a rolling 2-second window.
- HTTP 418/429/503 and MEXC JSON rate-limit responses are detected before normal API-error handling.
- Rate-limit retries use bounded exponential backoff, optional `Retry-After`, and small jitter.
- Backoff creates a shared pause so concurrent tasks do not immediately stampede the endpoint again.
- Symbol-level scanner concurrency is capped at 4 even if an older Render `SCAN_CONCURRENCY` value is higher.
- All rate-limit values are configurable through `MEXC_PUBLIC_*` and `MEXC_RATE_LIMIT_*` environment variables; no strategy thresholds were changed.

Verification for this replacement release:

```text
pytest -q
37 passed in 1.35s

python -m compileall -q app tests
OK
```
