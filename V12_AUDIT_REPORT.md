# KRONOS MEXC INTELLIGENCE V12 — Forensic audit

## Executive finding

The most likely direct cause of jobs staying at `QUEUED / 0%` was a worker startup import failure, not the WhatsApp webhook. The web process could accept `/webhook` and persist jobs, but a worker imports `app.services.pipeline` before starting its polling loop. `app.services.pipeline` imports `app.artifacts.store.DatabaseForecastArtifactStore`. The submitted canonical project tree did not contain `app/artifacts/`, although those exact files existed inside the ZIP's embedded WhatsApp replacement archive. The `.dockerignore` also had an unanchored `artifacts` rule, which could exclude `app/artifacts/` from Docker build context; `.gitignore` used the same unanchored directory rule, which could exclude it from Git staging. This combination explains how the API could stay healthy while all job workers failed before claiming jobs.

V12 restores the missing `app/artifacts/__init__.py` and `app/artifacts/store.py` files, whose SHA-256 values match the source archive's manifest, fixes both ignore patterns, and delays the pipeline import until after the worker writes a boot log. The worker now logs a clear `[WORKER_IMPORT_FAILED]` message if a required module is still missing.

This is a code-level root cause reproduced by importing the worker/pipeline and covered by tests. Actual Render recovery still depends on deploying the corrected image and ensuring the three Render worker services are active and share the web service's PostgreSQL database.

## Evidence from the supplied screenshots

- `HELP` returns a command list.
- `ANALYZE BTCUSDT` returns a job ID and `CHART BTCUSDT 1D` / `CHART BTCUSDT 1H` deliver chart images. This verifies the inbound command path and image-send path were functioning in the screenshot session.
- `BACKTEST 1D` returns a backtest job ID.
- `STATUS` shows an analysis job stuck at `QUEUED`, `0%`, `Update: Queued`. This is consistent with a role worker not claiming the row.
- The earlier log `POST /webhook 200 OK` only proves the webhook endpoint accepted the signed request; it does not prove a worker consumed the job or that the outbound response succeeded.

## Dependency map

```text
Meta webhook
  -> FastAPI /webhook or /api/whatsapp/webhook
  -> signature verification + inbound-event deduplication
  -> WhatsApp sender allowlist + command parser
  -> app.services.jobs.enqueue_job()
  -> PostgreSQL jobs table
       -> market worker: scan jobs
       -> inference worker: analysis / forecast / forecast evaluation
       -> compute worker: backtest jobs
  -> app.services.pipeline.process_job()
       -> MEXC public REST / WebSocket data
       -> candle validation + 1D / 12H / 4H / 1H strategy
       -> official Kronos source/model adapter (lazy load)
       -> database forecast artifact store
       -> analysis / backtest / forecast report persistence
  -> worker completion notification (status-only)
  -> authenticated React dashboard for detailed research output
```

A worker must be deployed for each role, and each role must use the exact same PostgreSQL `DATABASE_URL` as the web process. SQLite is for single-process local development, not a multi-container Render queue.

## Repairs implemented

### Queue / worker

- Restored the missing artifact-store package and fixed ignore rules so Git and Docker can include it.
- Worker imports the pipeline after emitting a boot log and records a traceback under `[WORKER_IMPORT_FAILED]` if import fails.
- Added worker/job structured logs with job ID, request ID, role, attempt and duration markers.
- Added persisted `next_attempt_at` plus Alembic migration `0003_job_retry_schedule`.
- Transient failures are rescheduled with exponential delay (15 seconds, 30 seconds, 60 seconds, then capped at 900 seconds) up to `max_attempts`; terminal failure is persisted rather than silently discarded.
- Expired leases are recovered to a retry or terminal failure state.
- Worker ownership is checked on progress, lease renewal, finish and failure writes so an old worker cannot overwrite a job reclaimed by another process.
- Inference and compute roles are isolated: analysis/forecast/evaluation are claimed by `inference`; backtests by `compute`; scans by `market`.
- Added a worker-loop regression test using a real database queue row and a mocked processing function.

### Status / observability

- `/healthz` is process liveness only; it no longer implies database or worker readiness.
- `/readyz` performs a cheap database `SELECT 1` instead of running `create_all` on every health check.
- Authenticated `/api/status` now reports queue counts, oldest queued-job age, last market snapshot, last forecast, worker heartbeats and latest failed job.
- WhatsApp `STATUS`, `QUEUE`, `WORKERS`, and `HEALTH`/`DIAGNOSTICS` return queue/worker context. The status lookup searches sender-owned jobs instead of only scanning a recent 150-row window.
- Kronos readiness is not inferred from a heartbeat alone: model weights load lazily and the API reports that a completed forecast is needed to verify the actual load/inference.

### WhatsApp

- `META_*` environment variables take priority over legacy `WHATSAPP_*` aliases, preventing an old legacy variable from overriding `META_PHONE_NUMBER_ID`.
- `render.yaml` uses the canonical `META_*` variables and shares message-sending credentials with worker services where completion notifications are sent.
- Commands include `PING`, `PRICE`, `ANALYZE`, `CHART`, `FORECAST`/`KRONOS`, `BACKTEST`, `STATUS`, `QUEUE`, `WORKERS`, `HEALTH`/`DIAGNOSTICS`, `DASHBOARD`/`SIGNALS`, and `SCAN`.
- `BACKTEST [symbol] [1D|7D|30D] [KRONOS]` supports an optional historical Kronos filter. Omitting `KRONOS` keeps it off by default.
- Added request-ID, parse, command, job-created/queued, chart, response and failure log markers.
- Detailed trading levels are not sent through WhatsApp. The authenticated dashboard remains the detailed research interface. Webhook signature verification, inbound deduplication and sender allowlisting remain enabled.

### Kronos / charts / evaluation

- The application pins the upstream Kronos source revision and Hugging Face model/tokenizer revisions. The inference worker image installs the optional inference dependencies and clones the pinned source. Model weights are fetched lazily on first inference and are not bundled in the ZIP.
- Individual model-generated OHLCVA paths are retained. Forecast summaries include median forecast OHLC by future step, direction from the median terminal return, path shares, horizon-scaled volatility and P10–P90 dispersion. Path shares are explicitly labeled uncalibrated sample frequencies, not market probabilities.
- The dashboard draws the observed candles separately from future median OHLC forecast candles, individual paths and P10–P90 bands; validated strategy support/resistance/entry/stop/TP1/TP2 levels can be overlaid.
- Forecast evaluation now compares Kronos against a last-value baseline and a simple drift baseline derived only from the prior 24 completed input bars. The result is one matured out-of-sample forecast evaluation, not a global calibration claim.
- Missing model source, dependencies or weights fail explicitly; no synthetic forecast fallback is used.

### Backtest

- Additional aggregate counts include tested symbols, processed candles by 1H/4H/derived-12H/1D, generated long/short candidates, opened trades and rejection counts.
- Trade P&L metrics distinguish closed trades from unresolved positions; unresolved positions are not counted as wins or losses.
- Reports include planned/realized R, net P&L, regime and timeframe groupings, and a zero-signal explanation.
- TP3 is explicitly reported as `NOT_IMPLEMENTED_IN_CURRENT_STRATEGY`; no TP3 result is invented.
- Kronos/technical/combined directional accuracy remains `null` in a trade-P&L backtest. The separate matured `ForecastEvaluation` records actual Kronos directional accuracy and forecast error against baselines.

## Strategy and data integrity retained

- Strategy timeframes remain **1D → 12H → 4H → 1H**.
- No 5M, 15M or 30M strategy decision timeframe was introduced.
- 12H candles remain deterministically aggregated from three contiguous completed 4H candles on UTC boundaries.
- Backtest decisions use only candles completed at the decision timestamp; entries are simulated at the next 1H candle open.
- MEXC K-line pagination uses bounded windows below the 2,000-point API response cap and rejects malformed/misaligned data.
- Live exchange order execution remains disabled.

## Known limitations / acceptance gaps

- The actual pretrained Kronos weights were not available in this local test runtime; a real model-load and forecast-candle generation test is **NOT TESTABLE** here. Render must download the pinned model/tokenizer into its persistent worker disk, and CPU time/RAM need measurement on the deployed plan.
- Live MEXC historical data retrieval and real backtest results were not run in this environment. Python backtest logic tests pass, but no market P&L is claimed.
- Live WhatsApp API delivery was not exercised in this environment. The command/API tests use mocks; a real message requires valid Meta credentials, registered number ID, app permissions and active webhook configuration.
- Frontend production build was not testable because npm dependencies were absent and package-registry installation timed out. TypeScript reported missing React/type packages, not a validated source-code build result.
- The Docker image was not built here. Worker startup imports and Render YAML syntax were checked, but a Render deployment is still required.
- RUFF lint could not be run because the `ruff` executable is not installed in this environment.
- TP3 is not implemented. Backtests use the current contract metadata and do not reconstruct point-in-time historical contract rules, book queue priority, full exchange partial fills or cross-margin liquidation.
- Forecast training-data cutoff is not independently verified; the code labels possible pretraining overlap rather than claiming it is excluded.

**Verdict:** the source-level queue startup blocker and the identified routing/observability gaps are repaired and regression-tested. This archive is not declared production-ready until the Render workers start, claim real jobs, the Kronos model loads successfully, and live external integrations are separately verified.
