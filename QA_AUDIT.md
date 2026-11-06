# V1.0.0 — Final engineering QA audit

Review date: 2026-11-06  
Scope: the exact source tree packaged with this release.  
This is an internal implementation review, not an independent certification or a claim of ten external auditors.

## Ten-area source and architecture review

| # | Review area | Status | Evidence and limits |
|---:|---|---|---|
| 1 | Scope and execution boundary | PASS | Fixed to `BTC_USDT` and `Min1`. Source scan found no private MEXC or order-placement/cancellation endpoint/method. Live service is alert-only. |
| 2 | EMA / RSI calculations | PASS | EMA uses a deterministic SMA seed; RSI uses Wilder smoothing. Tests cover seed, trend, flat data and signal conditions. |
| 3 | Candle closure and signal edge | PASS WITH LIMITS | Live monitor uses closed candles, warm-up, bootstrap skip and false-to-true condition edges. Startup deliberately suppresses the most recent signal rather than replaying history. |
| 4 | Abnormal movement and stale alert controls | PASS | Tests cover pause/recovery. Live alert is suppressed on stale ticker or >0.20% deviation from the signal candle close. Thresholds remain configurable assumptions, not a validated market-safety guarantee. |
| 5 | Contract sizing and metadata integrity | PASS | Contract size, tick, volume step, min/max size, max leverage and fee/rate metadata are validated. Missing required fee metadata now fails closed; max margin budget is capped at 0.50 USDT and quantity floors to the exchange step. |
| 6 | Historical data retrieval and validation | PASS WITH LIMITS | Fresh segmented candle requests, 2,000-point pagination, and gap/duplicate/invalid/out-of-order/error checks exist. Tests use mocked HTTP responses; live MEXC API connectivity was not tested in this environment. |
| 7 | Backtest model and metric honesty | PASS WITH LIMITS | Four RSI configurations × three TP targets are evaluated. TP-touch, unresolved, assumed fee and slippage are explicit. Historical executable bid/ask, liquidation and funding are not modelled; win rate, PF, drawdown and total strategy profitability are withheld. |
| 8 | WhatsApp webhook and delivery boundary | PASS WITH LIMITS | Raw-body HMAC check, verification challenge, sender allowlist, inbound-message dedupe, payload cap and text chunking are implemented. Actual Meta delivery and account configuration were not tested. |
| 9 | State, task and recovery behavior | PASS WITH LIMITS | SQLite WAL stores message/signal dedupe and monitor state; tasks are tracked and backtests serialized. This is a single-instance design; horizontal scaling/multi-instance locking is not supported. |
| 10 | Deployment and source packaging | PASS WITH LIMITS | Dockerfile, Render Blueprint, persistent disk config, Procfile, environment example and GitHub Actions workflow are included. Docker build, GitHub push and Render deployment were not run because Docker/account access is unavailable here. |

## Automated verification

- Automated suite: **32 tests passed**.
- Repeatability: **10 consecutive full-suite runs passed** (32/32 each run; 320 successful test executions).
- `python -m compileall -q app tests`: PASS.
- 30-day synthetic workload: 43,320 one-minute candles, 12 RSI/TP variants, 5,465-character output, approximately 1.267 seconds on the local fixture. This is a synthetic CPU benchmark only, not live API speed or trading performance.
- Docker CLI: unavailable; image build NOT RUN.
- Live MEXC REST integration: NOT RUN.
- Live WhatsApp send/webhook round trip: NOT RUN.
- Render deployment and persistence/restart validation: NOT RUN.
- Dedicated Ruff lint: NOT RUN (Ruff is not installed and package installation was unavailable). Compilation and test suite passed.
- The container's global `pip check` reported an unrelated preinstalled `moviepy`/Pillow version conflict; neither package is part of this project's pinned requirements.

## Critical model limitations

1. No liquidation reconstruction, account-specific risk-tier simulation or funding cashflows.
2. No historical bid/ask or order-book replay. Next-candle open ± one tick is only a disclosed entry/exit approximation.
3. A 1-minute candle cannot establish the order of intrabar events or prove an executable fill.
4. TP touches are not confirmed wins. Reported TP-subset PnL excludes unresolved outcomes and is not total strategy PnL.
5. There is no normal stop-loss. An unresolved hypothetical backtest position blocks new entries through the sample end.
6. The bot has no live position awareness and does not place or manage any exchange order.
7. The 100 USDT figure is reference context only; the backtest does not compound account equity or model whole-account drawdown.

## Release operator checklist

1. Keep the GitHub repository private and do not upload `.env`, access tokens or app secrets.
2. Add Meta credentials only in Render environment settings; use a dedicated authorized sender allowlist.
3. Confirm Render plan and persistent-disk charges before applying the Blueprint.
4. Confirm the Graph API version enabled for the owner's Meta app; `v26.0` is only the configured default.
5. After deployment, verify `/healthz`, webhook verification, `/help`, `/status`, and one controlled market-data/backtest request.
6. Do not enable or infer trading execution; no order-placement code exists in this release.

No WhatsApp policy websites were consulted. Only technical API integration documentation was referenced.
