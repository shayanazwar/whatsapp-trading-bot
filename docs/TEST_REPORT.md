# v0.7.0 Test & Validation Report

**Date:** 1 November 2026  
**Source:** supplied `whatsapp-trading-bot-main (39).zip`, patched in a clean working extraction.

## Automated verification

| Check | Result |
|---|---:|
| `python -m compileall -q app tests` | PASS |
| `python -m pytest -q` | **129 passed** |
| FastAPI application import | PASS |
| Application version | `0.7.0` |
| Render Blueprint YAML parse/structural assertions | PASS |
| Archive/candle aggregation tests, including hot-shard cache bound | PASS |
| Historical Kline pagination regression | PASS |
| Top-100 universe filtering/fallback tests | PASS |
| Strategy B warm-up, gap and trigger tests | PASS |
| Fee/slippage net-RR parity tests (LONG and SHORT) | PASS |
| Worker health/heartbeat tests | PASS |
| PostgreSQL job-store / idempotency tests | PASS |
| Cross-margin ledger and report reconciliation tests | PASS |
| ZIP integrity | Verified during packaging |

## Additional local benchmark

A synthetic Python allocation test created 100 symbols × 4,610 3M-candle objects (461,000 candle objects; approximately 7D plus candidate warm-up). `tracemalloc` peak was approximately 84.4 MiB for that narrow allocation test.

This is **not** a full historical backtest benchmark, process RSS measurement, or Render memory guarantee. It does not include HTTP response buffers, database drivers, pandas/numpy data, fair-price/funding data, or the complete job pipeline.

## Environment caveats

- `ruff` was not installed in the execution environment, so no Ruff lint result is claimed.
- Global `pip check` reported an unrelated pre-existing environment conflict: `moviepy 2.2.1` requires Pillow `<12`, while the environment has Pillow `12.3.0`. Pillow/moviepy are not project runtime dependencies in this patch. The project test suite still passed.

## Not performed

- No live MEXC API calls or account-specific fee/tier verification.
- No actual 100-symbol scan against live MEXC.
- No actual MEXC 1D/7D/30D backtest result generated in this session.
- No Render deployment, memory-limit observation, database provisioning or disk-mount check.
- No Meta/WhatsApp production message test.
- No live trading or order placement.

## Interpretation

The code compiles and the included automated tests pass. Tests use deterministic fixtures and mocks for external APIs. Passing tests establishes behavior under those fixtures; it does not establish live data coverage, trading profitability, exact exchange-liquidation parity, or deployment success.
