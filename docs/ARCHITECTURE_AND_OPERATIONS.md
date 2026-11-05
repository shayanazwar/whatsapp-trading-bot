# Architecture and Operations — v0.8.1

## Release scope

Research-only MEXC USDT perpetual system. Active strategy: **S1-RFPC**, using completed 1D → 12H → 4H → 1H candles only. 12H bars are deterministically aggregated from completed 4H bars on UTC boundaries. Live exchange order placement is not implemented/released.

## Runtime services

The Render Blueprint omits explicit compute `plan` fields to preserve the current plans on existing resources and avoid hardcoded legacy plan IDs. Review any Render Blueprint changes before applying them.

```text
WhatsApp / protected dashboard
        |
        v
FastAPI web service
  - inbound signature verification
  - command parsing/authorization
  - durable BACKTEST job enqueue
  - immediate QUEUED acknowledgement
  - /healthz diagnostics
        | shared DATABASE_URL
        v
PostgreSQL
  - inbox idempotency
  - backtest_jobs + progress/results
  - app_state worker heartbeat/build/database fingerprint
  - signals/events/outbox
        ^
        |
Dedicated Render background worker
  - startup + heartbeat
  - backtest queue polling (BACKTEST_JOB_POLL_SECONDS; default 5s)
  - top-universe scanner on scheduler cadence
  - S1-RFPC history loading and backtest simulation
  - gated outbox delivery
```

The web service and worker must deploy the same Git commit and use the same PostgreSQL `DATABASE_URL`. The worker also mounts the persistent market archive disk. The disk is worker-local and is not a substitute for PostgreSQL job sharing.

## Worker lifecycle

The web service inserts the job and returns `BACKTEST JOB QUEUED`. Only the dedicated worker claims it. On claim, worker logs include `BACKTEST JOB START`; the strategy command then emits `BACKTEST START`, `BACKTEST EXCHANGE TIME REQUEST START`, `BACKTEST UNIVERSE LOAD START`, `BACKTEST SYMBOL SCAN START`, and `BACKTEST SYMBOL SCAN | progress=n/N`. A completion or failure is persisted in `backtest_jobs`.

Queue polling is independent from market scan cadence. A slow top-universe scan must not delay queue polling for a full 60-second scan interval. The worker serializes backtests (one active backtest consumer task) to avoid running multiple heavy jobs against the same MEXC client/archive at once.

## Health diagnostics

`GET /healthz` distinguishes: `scanner_health` (the optional scheduled signal scanner) and `worker_health` (the job-consuming worker). `worker_health` is not hidden merely because the optional scanner flag is off. Key diagnostics: `scanner_worker_status`, `worker_heartbeat_age_seconds`, `worker_build_version`, `worker_database_match`, `worker_database_fingerprint`, `backtest_job_poll_seconds`, `backtest_latest_job.status`, and error/progress fields.

Healthy worker requires a fresh heartbeat and `worker_status=running`. `worker_database_match=true` confirms that the stored worker fingerprint matches the web service's database identity. A missing heartbeat means no worker identity has been recorded; a queued job is not proof of execution.

## Strategy and research portfolio

- 1D: broad regime.
- 12H: market structure.
- 4H: setup/location.
- 1H: entry trigger.
- Only completed candles are consumed by strategy evaluation.
- Research backtest cap: `BACKTEST_MAX_POSITIONS` (default 50).
- Research aggregate planned stop-risk cap: `BACKTEST_MAX_OPEN_RISK_FRACTION` (default 25%).
- Separate paper engine cap: `MAX_CONCURRENT_POSITIONS=1`.
- The 50-position value is a ceiling, not a target; risk, notional, contract minimums, same-symbol and strategy rules can reject entries.

## WhatsApp delivery

All supported WhatsApp commands and research notifications are routed for explicitly allowlisted senders. Webhook HMAC verification, sender allowlisting, message-ID idempotency, valid Meta credentials and the monthly outbound cap remain active. Signal delivery additionally requires the strategy's data-quality, contract, liquidity and net reward/risk checks to pass. Live MEXC order placement remains unavailable and hard-locked off.

## Fundamentals

Tokenomics/on-chain fundamentals are not implemented. `ANALYZE` now says `NOT_IMPLEMENTED` explicitly. Funding, open interest, contract details, depth and turnover are derivatives/market microstructure metrics and must not be relabeled as token fundamentals. See `docs/FUNDAMENTALS_DATA_AUDIT.md`.

## Deployment verification

1. Deploy the same commit to `mexc-rsi-ema-web` and `mexc-rsi-ema-scanner`.
2. Confirm the worker's start command is `python -m app.worker`.
3. Confirm both services use the same PostgreSQL `DATABASE_URL`.
4. Confirm the worker log contains `MEXC research worker starting` and `BACKTEST QUEUE POLLER READY`.
5. Check `/healthz` for `worker_health=healthy`, a heartbeat age below 75 seconds, and `worker_database_match=true`.
6. Send one `BACKTEST 1D`; watch the worker logs for `BACKTEST JOB START`, `BACKTEST START` and symbol progress.
7. Use `BACKTEST STATUS`, then `BACKTEST REPORT <JOB_ID>`.

## Safe operating posture

Keep `AUTO_TRADING_ENABLED=false`, `LIVE_EXECUTION_ENABLED=false`, `LIVE_EXECUTION_BUILD_RELEASED=false` and `PAPER_TRADING_ENABLED=false` until separately reviewed. This release does not implement a live order placement client.
