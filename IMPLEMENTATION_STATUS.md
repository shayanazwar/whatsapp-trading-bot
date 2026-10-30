# Implementation Status — v0.5.0 · 3M LONG/SHORT Cross-Margin Research Build

## Current code state

| Area | State | Limits |
|---|---|---|
| Universe | Top 100 active USDT perpetual contracts ranked by 24-hour turnover | The universe is selected at run time; using today's top-100 universe for historical windows introduces survivorship bias. |
| Candle interval | Completed 3-minute candles aggregated from three contiguous 1-minute MEXC candles | MEXC does not provide a native 3M interval. Gaps remain gaps; no synthetic filler candles are created. |
| Strategy | Symmetric LONG and SHORT RSI/EMA momentum rules (RSI 70/30, EMA 21/50/100, breakout/breakdown and volume confirmation) | Strategy is a hypothesis, not evidence of profitability. |
| Backtest | Cross-margin-aware, one-active-position research simulator with contract metadata, risk-tier limits, fees, slippage, funding and Fair Price liquidation checks where data is available | Synthetic market fills; OHLC candles cannot reproduce exact intrabar path, order-book impact, queue position, partial fills, or account-wide external positions/open orders. |
| Memory | Historical candles load with two in-flight symbol requests; Fair Price/funding history loads only for symbols actually opened by the simulator; raw 1M support bars are discarded after 3M aggregation | Long runs remain CPU/network intensive and are still executed by the web process; background-job migration is a future resilience improvement. |
| Scanner worker | Dedicated Render worker scans once per completed 3M slot; every symbol outcome, including data errors, is persisted | Render must deploy the updated Blueprint/environment. Existing manual Render settings may override Blueprint values. |
| Health diagnostics | `/healthz` exposes build version, scanner enablement, worker status/heartbeat, last attempt/success, summary and last error | Worker and web service must share the configured PostgreSQL database for health visibility. |
| Live execution | Disabled; no exchange order placement is implemented in this research path | Do not enable live trading based on backtest output. |
| WhatsApp | Webhook acknowledges inbound messages and processes commands in a background task | The Render memory-limit email is an infrastructure alert, not an outbound-email feature failure. |

## Forensic fixes in this patch

1. Render Blueprint now sets `MEXC_UNIVERSE_TOP_N=100` and `MEXC_UNIVERSE_SCAN_ENABLED=true`; the worker previously skipped `_universe_scan_tick()` because this setting defaulted to false and was absent from the Blueprint.
2. Backtest market-history downloads are limited to two concurrent symbols instead of eight.
3. Fair Price/funding support is loaded only for symbols that receive simulated positions, not every symbol that generated a raw signal. Support loading is sequential, and raw 1M Fair Price candles are discarded after 3M aggregation.
4. When a Fair Price-driven liquidation opens a later position on another symbol, the backtest detects the new traded symbol, loads its support data, and replays the simulation. The replay stops after a bounded number of passes and flags incomplete support data.
5. The scanner persists `DATA_ERROR` outcomes per pair instead of silently dropping failed symbols. Scan logs and `/healthz` now expose its operational state.
6. The dashboard text and shortcuts have been aligned with the current 3M strategy and supported commands.
7. A single build identifier (`0.5.0`) is exposed by FastAPI and `/healthz`; the worker logs it at startup. A 20-second worker heartbeat is persisted so operators can distinguish an active worker from a stale `running` state.

## Source/deployment identity warning

The user-supplied V38 ZIP identifies itself as version `0.4.0` and describes a LONG-only strategy. Its `app/backtest_rsi_ema.py` also sets `isolated_margin=True` in the report metadata. The live WhatsApp screenshots supplied separately show both LONG and SHORT signals and `Margin: CROSS`. Therefore the attached ZIP is not the same source revision as the running Render service that produced those screenshots (or the deployed service has code that is not present in the ZIP). The ZIP contains no `.git` directory and its README contains a placeholder GitHub remote URL. The connected GitHub account exposed no repositories in this session, so the deployed private branch and commit could not be inspected.

## Verification boundary

- Local source compilation and automated tests are run on this patch.
- No real 100-symbol MEXC scan, real 7D exchange download, Render deployment, or memory benchmark on the user's Render instance was performed in this environment.
- The Render email is the notification for the web process exceeding its memory limit. This patch reduces the identified peak-memory drivers; it cannot confirm that the service has recovered until the updated build runs on Render.
- For exact production verification, the deployed private GitHub repository and branch/commit must be accessible, and Render must deploy this exact patch.
