# Pak Trading Academy — v0.7.0 Implementation & Verification Report

**Date:** 1 November 2026  
**Scope:** Apply the approved research-only architecture to the supplied source archive. No live orders were placed, no production services were changed, and no live exchange/Meta account was accessed.

## Executive summary

The supplied project has been updated to a research-oriented architecture with a top-100 MEXC Futures universe, completed 1-minute-to-3-minute aggregation, a 15-minute context / 3-minute pullback-reclaim candidate strategy, a preserved RSI/EMA control strategy, shared PostgreSQL state for web and worker services, a worker-owned persistent candle archive, durable backtest jobs, and a portfolio-aware one-position backtest model.

The patch is locally compiled and the automated suite passes. This is **not** evidence of live MEXC data coverage, Render deployment health, real account fee settings, or positive trading expectancy. Those require deployment and shadow validation.

## Implemented architecture

### Services and persistence

- **FastAPI Web Service:** command/webhook ingress, health reporting, job enqueue, and report retrieval. It does not run the heavy backtest in the webhook request.
- **Background Worker:** owns scheduled scanning and durable backtest-job execution. It acquires a database-backed worker lock and claims jobs through PostgreSQL.
- **PostgreSQL:** central source of truth for jobs, signals, scan outcomes, idempotency and worker heartbeat, so Web and Worker do not depend on separate local SQLite files.
- **Worker persistent disk:** `/var/data` is attached to the Worker only. It stores compressed, date-partitioned 1M/3M candle archive files. The Web Service does not read that local path.
- **MEXC client:** requests historical Kline windows in bounded pages (up to configured API page cap), with chronology checks. A 3M candle is emitted only from three complete, unique, contiguous, UTC-aligned 1M candles.

### Top-100 scanner

- Discovers active eligible USDT perpetual contracts, ranks by 24-hour turnover, and caps the universe at 100.
- Uses bounded requests and a bulk ticker path with per-symbol fallback if the bulk response is incomplete.
- Persists per-symbol scan outcomes (`SIGNAL`, `NO_SIGNAL`, `DATA_ERROR`, `INSUFFICIENT_HISTORY`, or skip/error states) and scan-cycle summaries.
- Candidate signal delivery eligibility checks market quality, funding context, contract tick rounding, and modeled net reward/risk. A signal is not marked deliverable when the contract price tick is unavailable.
- Worker heartbeat and scanner health are exposed through `/healthz`.

### Strategy research

- **Strategy A (control):** existing RSI/EMA strategy remains available as a control; it is not silently replaced.
- **Strategy B (candidate):** 15M EMA trend context with 3M pullback/reclaim, ATR-normalized structural stop/target, RSI confirmation/veto, quote-volume participation, and quality scoring.
- Strategy B is a research candidate, not a proven profitable strategy. Thresholds are configurable research hypotheses and require out-of-sample/ablation testing.
- Strategy A and B are compared under the same configured costs and portfolio simulator.

### Backtest and risk model

- BACKTEST requests are durable database jobs claimed by the Worker, not synchronous heavy jobs inside FastAPI.
- Period is explicit; unsupported/uncovered windows should fail with a coverage error rather than silently returning a shorter period.
- Top-100 portfolio backtests are limited to the supported short windows; longer horizon is single-symbol only to bound cost and data volume.
- Historical windows are fetched in paginated 1M requests, cached to the worker archive, then aggregated to completed 3M candles. The report records requested window and actual candle coverage.
- Portfolio simulation is one active bot position at a time, with a shared cross-wallet balance, initial margin cap, risk-based candidate sizing, leverage cap, maintenance-margin tier inputs, fees, slippage, funding and mark/fair-price liquidation checks where data is available.
- The simulator is an approximation. It does not reproduce the complete MEXC account engine, external positions/orders, partial liquidation/ADL, all order-book queue behavior or the exact exchange matching engine.
- Intra-3M TP/SL ambiguity is resolved using constituent 1M bars when available; remaining same-1M ambiguity is handled conservatively.
- Report metrics are generated from the ledger and reconciliation checks validate signal/trade counts, R totals, equity, fees/funding and liquidation event counts.
- JSON, CSV and text report artifacts are written to the Worker report directory. They are not automatically attached to a WhatsApp message.

## Configuration and deployment shape

The Render Blueprint defines:

1. PostgreSQL database.
2. FastAPI Web Service with scheduler disabled.
3. Dedicated Worker with scanner enabled and the persistent `/var/data` disk.

The Worker explicitly sets its process role to `worker`; it owns the local candle archive. The Web Service must not be configured to use the Worker's local disk path.

Required deployment settings include a valid PostgreSQL connection URL, scanner universe limit 100, scanner enabled on the Worker, and both live execution and WhatsApp crypto messaging disabled.

## WhatsApp restriction

The project keeps the crypto-signal WhatsApp Business delivery gate **closed**. Scanner results and validated signals can be stored and retrieved through protected application/report interfaces, but this patch does not activate automatic crypto signal pushes through WhatsApp Business. The general WhatsApp command interface remains subject to existing policy checks. Do not manually override the restriction flag.

## Known limitations and unresolved validation

1. No live MEXC API session was run during this work. Pagination, contract filters, candle responses, funding and fair-price handling are covered by mocks/unit tests only.
2. No Render deployment was performed. The Render YAML was structurally validated locally, but actual Blueprint application, environment secrets, Postgres provisioning, disk mounting and service startup must be checked after deployment.
3. No 1D/7D/30D real-market backtest was executed here. The patch provides the engine and will refuse insufficient coverage; it does not invent historical data.
4. The current top-100 universe is selected using the current turnover ranking when replaying historical windows unless point-in-time universe snapshots have already been archived. Reports disclose this survivorship/selection-bias limitation.
5. Cross-margin liquidation is a single-bot-position research approximation using contract margin-tier inputs and available fair-price data. It is not an exchange-certified account simulator.
6. Funding history and fair-price candle coverage may be incomplete by symbol/window. Missing coverage must be disclosed; do not interpret missing funding as proof that funding cost was zero.
7. Account-specific fees, contract tick/lot sizes, risk tiers and leverage caps must be refreshed from MEXC/account metadata before relying on an execution estimate.
8. PostgreSQL is required for separate Web/Worker coordination. If deployed without a valid shared database, the service must not be considered correctly configured.
9. Persistent worker archive files are not shared with the Web Service. Reports should be retrieved from database metadata or the protected report endpoint; local archive paths are worker-local.
10. Profitability is unproven. Candidate strategy parameters are hypotheses; require walk-forward and shadow evaluation before considering any live system.

## Deployment verification checklist

After deploying the same commit to Web and Worker:

- Confirm `/healthz` returns build `0.7.0`, DB ready and a current Worker heartbeat.
- Confirm the Worker has `/var/data` mounted and the Web Service does not claim that local disk.
- Confirm Worker logs show top-100 scanner startup and completed 3M cycles.
- Confirm a cycle records 100 discovered/processed outcomes or explicitly logs any data failures.
- Confirm the Worker archive grows over time and has no duplicate 1M timestamps per symbol.
- Run BACKTEST 1D first; verify period start/end, coverage, symbols, costs and trade ledger.
- Run BACKTEST 7D only after the archive has at least seven days of contiguous history for the required universe. If coverage is missing, the job should fail clearly, not substitute 1D.
- Compare control and candidate with identical universe, timestamps, fees, slippage and risk assumptions.
- Keep live execution and WhatsApp crypto messaging disabled.

---

**Final status:** local source changes and tests complete; live deployment and real-market outcome remain unverified.
