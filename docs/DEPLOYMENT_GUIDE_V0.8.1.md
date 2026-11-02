# Render Deployment — v0.8.1

## Services

The Render Blueprint defines:
- `mexc-rsi-ema-web` — Docker web service running `uvicorn app.main:app` on `$PORT`.
- `mexc-rsi-ema-scanner` — Docker background worker running `python -m app.worker`.
- `mexc-rsi-ema-db` — PostgreSQL shared by both services.

Keep the existing Render resources and database when replacing source code. Do not delete the database or recreate services solely to update the repository. Ensure the web service and worker deploy the same Git commit and inherit the same `DATABASE_URL`.

## Resource safety

The Blueprint intentionally omits explicit Render `plan` overrides. Render retains the current compute plan for existing services/databases; new resources use current platform defaults. Do not add legacy plan IDs such as `starter` or `basic-256mb` without validating them against the current Render Blueprint specification. A Blueprint sync can change infrastructure, so review its proposed changes before applying.

## Required checks

1. The source tree at the repository root must contain `app/`, `tests/`, `Dockerfile`, `render.yaml`, `pyproject.toml` and `requirements-runtime.lock`.
2. The worker's start command must be `python -m app.worker`.
3. Worker logs should show `MEXC research worker starting` and `BACKTEST QUEUE POLLER READY`.
4. `/healthz` should report `worker_health=healthy`, `worker_heartbeat_age_seconds < 75`, and `worker_database_match=true`.
5. After a backtest command, logs should show `BACKTEST JOB START`, `BACKTEST START`, `BACKTEST UNIVERSE LOAD START` and `BACKTEST SYMBOL SCAN | progress=1/N`.

`SCHEDULER_ENABLED=false` on the web service is intentional: the dedicated worker runs the research scheduler independently. It does not turn off the worker's backtest queue consumer. The `BACKTEST_JOB_POLL_SECONDS` setting controls backtest queue polling separately from the market-scan cycle.

## WhatsApp

`SIGNAL_PUSH_ENABLED` alone does not authorize crypto message delivery. The separate `WHATSAPP_CRYPTO_MESSAGING_ALLOWED` + `WHATSAPP_POLICY_CLEARANCE_REF` gate, allowlisted recipient and valid Meta configuration are required. Keep the gate closed until your actual business use is covered by applicable written clearance.
