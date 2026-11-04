# KRONOS MEXC INTELLIGENCE V12 — deployment and rollback

## What this release fixes

The submitted project could accept and persist queued jobs while a worker failed during startup because `app.artifacts` was absent from the canonical project root. The package is restored, and `.gitignore` / `.dockerignore` now exclude only the root artifact-cache directory, not the required Python package. The deployment must include all three worker roles below; deploying only the web service will still leave jobs queued.

## Before replacing the repository

1. Back up the current GitHub repository and note the current Render deployment IDs.
2. Back up the PostgreSQL database using Render's backup/export facility.
3. Keep the new V12 project in a private repository. Do not commit `.env`, database files, API tokens, passwords, model weights or local caches.
4. Replace the repository contents with this ZIP's root contents. Confirm `app/artifacts/store.py` is present before pushing.

## Git deployment

From the repository root (the folder containing `render.yaml`, `Dockerfile.web` and `app/`):

```bash
unzip -o KRONOS_MEXC_INTELLIGENCE_V12_FINAL_FIXED.zip -d .
git add -A
git commit -m "Deploy KRONOS MEXC Intelligence V12 queue and forecast fixes"
git push
```

If using a new repository, create it as private, add the remote and push the current branch. Do not extract this archive inside the old nested `KRONOS-MEXC-INTELLIGENCE/` duplicate folder.

## Render services required

The supplied `render.yaml` describes these services and a shared PostgreSQL database:

| Service role | Dockerfile | Start command | Queue types |
|---|---|---|---|
| Web | `Dockerfile.web` | `python -m app.migration_lock && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}` | Receives API/webhook requests; does not consume jobs |
| Market worker | `Dockerfile.market` | `python -m app.migration_lock && python -m app.worker --role market` | `scan` |
| Compute worker | `Dockerfile.compute` | `python -m app.migration_lock && python -m app.worker --role compute` | `backtest` |
| Inference worker | `Dockerfile.compute` | `python -m app.migration_lock && python -m app.worker --role inference` | `analysis`, `forecast`, `evaluate_forecast` |

All four services must use the exact same PostgreSQL `DATABASE_URL`. The `render.yaml` also provisions a PostgreSQL database. If you are updating an existing Render service named `shayan-millionaire-bot`, do not create a duplicate stack blindly: either apply the Blueprint after reviewing each service or update the existing services individually to use the Dockerfiles and role-specific start commands above. A worker showing `Live` in Render is not enough; confirm its fresh `[WORKER_STARTED]` log and heartbeat.

The compute and inference roles need the `Dockerfile.compute` image with pinned Kronos source and inference dependencies. Each service has its own persistent `/var/data` model cache; one worker's disk is not shared with another. The inference worker's disk/cache is the relevant one for `ANALYZE` / `FORECAST` / `KRONOS` jobs.

## Required environment variables

Set these on the web service and relevant worker services. Use the same database URL on every service.

```env
APP_ENV=production
APP_SECRET_KEY=<long random secret, at least 32 characters>
ADMIN_USERNAME=<your admin username>
ADMIN_PASSWORD=<strong unique password>
DATABASE_URL=<Render PostgreSQL internal connection string>
PAPER_MODE=true
LIVE_TRADING_ENABLED=false
AUTOMATIC_ORDER_EXECUTION=false
WHATSAPP_ENABLED=true
META_PHONE_NUMBER_ID=1343308352199390
META_ACCESS_TOKEN=<new token generated after revoking the token previously pasted into chat>
META_VERIFY_TOKEN=<your existing webhook verification token>
META_APP_SECRET=<Meta app secret>
WHATSAPP_ALLOWED_RECIPIENTS=923489992960
WHATSAPP_DEFAULT_RECIPIENT=923489992960
PUBLIC_BASE_URL=https://shayan-millionaire-bot.onrender.com
HF_HOME=/var/data/huggingface
FORECAST_DEVICE=cpu
FORECAST_SAMPLE_COUNT=1000
FORECAST_HORIZON=24
```

`META_PHONE_NUMBER_ID=1343308352199390` is the production ID you supplied. Do not use the test-number ID. `META_*` takes precedence over legacy `WHATSAPP_*` aliases; remove stale legacy credential values from Render to avoid confusion. Never paste `META_ACCESS_TOKEN`, `META_APP_SECRET`, admin credentials or database URLs into GitHub source or chat.

`META_VERIFY_TOKEN` and `META_APP_SECRET` are needed by the web webhook. `META_ACCESS_TOKEN` and `META_PHONE_NUMBER_ID` are needed by processes that send WhatsApp notifications (web and workers). `WHATSAPP_ALLOWED_RECIPIENTS` must contain the normalized sender number(s) permitted to issue commands. Keep paper mode and live execution flags as shown; V12 does not enable live MEXC execution.

## Deploy verification sequence

1. Wait until the web, market, compute and inference services are all deployed from the same commit.
2. Confirm web `/healthz` is HTTP 200 (process liveness) and `/readyz` succeeds (database readiness).
3. On each worker, confirm `[WORKER_STARTED]` and subsequent heartbeat messages. Confirm role names: `market`, `compute`, `inference`.
4. Send WhatsApp `PING`, then `WORKERS`, `QUEUE`, and `HEALTH`.
5. Send `ANALYZE BTCUSDT`. The inference worker should log `[WORKER_PICKED]`, `[JOB_STARTED]`, `[DATA_FETCHED]`, then `[KRONOS_STARTED]` / `[KRONOS_COMPLETED]` if the pinned model loads successfully. Query `STATUS <job-id>`.
6. Send `BACKTEST BTCUSDT 1D` and query `BACKTEST STATUS` (or the reported job ID using `STATUS <job-id>` where the command supports it). The compute worker should claim it and show backtest lifecycle logs.
7. Use `BACKTEST BTCUSDT 1D KRONOS` only after the inference worker can load model weights. This is a historical filter and may be CPU/memory intensive.
8. Check the dashboard for completed forecast paths and future forecast candles. A chart image sent by WhatsApp is only the completed-candle chart; forecast visualization is in the dashboard.

A job is not considered working merely because WhatsApp says “queued.” It must move to `RUNNING` and then `COMPLETED` or a visible terminal `FAILED` state. If the queue still grows, check all worker service logs and verify their `DATABASE_URL` matches the web service. If a worker prints `[WORKER_IMPORT_FAILED]`, capture the full traceback. If it logs `[KRONOS_NOT_READY]` / model-load failure, the queue can execute but the model weights, network access, dependencies or memory budget are still unresolved.

## Rollback

1. In Render, redeploy the previous known-good deploy for the affected service(s), or revert the V12 Git commit:

   ```bash
   git revert <v12-commit-sha>
   git push
   ```
2. Redeploy web and all workers from the rollback commit so they do not run mixed code versions against the same queue.
3. Keep the additive `0003_job_retry_schedule` database column during rollback. The previous application version should ignore the additional column; do not manually drop it or downgrade the migration while any V12 service may still be running.
4. Restore the database backup only if data/schema corruption is established. The V12 migration is additive; routine code rollback does not require a database restore.
5. Rotate any credentials that were exposed. Do not restore a previously disclosed access token.

## Important limitations

- Local tests do not prove that Render workers have started, that the live MEXC API is reachable, that a real WhatsApp message is delivered, or that the Hugging Face Kronos weights fit the deployed CPU/RAM plan.
- The pinned Kronos source and model/tokenizer revisions are configured, but model weights are downloaded lazily on first inference and are not bundled in this ZIP.
- Forecast path proportions are uncalibrated sample frequencies, not calibrated real-world probabilities. Out-of-sample calibration requires matured walk-forward observations.
- TP3 is not implemented in the current strategy. Live trading remains disabled.
