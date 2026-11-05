# Architecture and Operations — v0.8.1

## Release scope

Research-only MEXC USDT perpetual system. Active strategy: **S1-RFPC**, using completed 1D → 12H → 4H → 1H candles only. 12H bars are deterministically aggregated from completed 4H bars on UTC boundaries. Live exchange order placement is not implemented/released.

## Runtime services

The Render Blueprint uses one web service (one Uvicorn worker) plus PostgreSQL. The FastAPI lifespan starts a queue-only backtest consumer in the same process and, when `SCHEDULER_ENABLED=true`, a separate asynchronous market-scanner loop. A PostgreSQL advisory lock prevents a second in-process instance or an old standalone worker from consuming jobs concurrently.

```text
MEXC market data
      |
      v
Single Render web service
  - webhook/dashboard + command parser
  - durable BACKTEST job enqueue
  - in-process queue consumer (one job at a time)
  - optional top-universe scanner/paper scheduler
      |
      v
PostgreSQL
  - backtest_jobs + progress/results
  - app_state heartbeat/build/database fingerprint
  - events/signals/outbox
```

No separate Render background worker is required. Stop/delete an already deployed `mexc-rsi-ema-scanner` service before deploying this topology. Keep one web instance and one Uvicorn worker so the advisory lock and local archive/cache assumptions stay consistent.

## In-process backtest queue lifecycle

The web service inserts the job and returns `BACKTEST JOB QUEUED`. A queue-only consumer in the same FastAPI process claims it; no separate Render worker is required. On claim, web-service logs include `BACKTEST JOB START`; the strategy command then emits `BACKTEST START`, `BACKTEST EXCHANGE TIME REQUEST START`, `BACKTEST UNIVERSE LOAD START`, `BACKTEST SYMBOL SCAN START`, and `BACKTEST SYMBOL SCAN | progress=n/N`. A completion or failure is persisted in `backtest_jobs`.

Queue polling is independent from market scan cadence. A slow top-universe scan must not delay queue polling for a full 60-second scan interval. The in-process consumer serializes backtests (one active job) to avoid running multiple heavy jobs against the same MEXC client/archive at once. A PostgreSQL advisory lock blocks an old standalone worker or a second web instance from becoming another consumer.

## Health diagnostics

`GET /healthz` distinguishes: `scanner_health` (the optional scheduled signal scanner) and `worker_health` (the job-consuming worker). `worker_health` reports the in-process queue consumer even when the optional scanner flag is off. `scanner_health` separately reports whether the scheduled market scanner is enabled. Key diagnostics: `scanner_worker_status`, `worker_heartbeat_age_seconds`, `worker_build_version`, `worker_database_match`, `worker_database_fingerprint`, `backtest_job_poll_seconds`, `backtest_latest_job.status`, and error/progress fields.

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

1. Deploy the commit to `mexc-rsi-ema-web` only; stop/delete any old `mexc-rsi-ema-scanner` background worker.
2. Keep one web instance and one Uvicorn worker.
3. Check `/healthz` for `in_process_backtest_queue=true`, `backtest_executor_status=running`, `worker_health=healthy`, a heartbeat age below 75 seconds, and `worker_database_match=true`.
4. Send one `BACKTEST 1D`; watch the web service logs for `IN-PROCESS BACKTEST QUEUE READY`, `BACKTEST JOB START`, `BACKTEST START` and symbol progress.
5. Use `BACKTEST STATUS`, then `BACKTEST REPORT <JOB_ID>`.

## Safe operating posture

Keep `AUTO_TRADING_ENABLED=false`, `LIVE_EXECUTION_ENABLED=false`, `LIVE_EXECUTION_BUILD_RELEASED=false` and `PAPER_TRADING_ENABLED=false` until separately reviewed. This release does not implement a live order placement client.
