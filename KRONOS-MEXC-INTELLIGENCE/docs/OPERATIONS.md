# Operations

- Run web, market and compute as independent processes.
- Watch `GET /healthz`, `GET /readyz`, authenticated `/api/status`, `/api/jobs` and `/api/events`.
- Alert on stale market timestamps, failed jobs, repeated MEXC 429/5xx responses, worker heartbeat expiry, model-load failure and DB connection errors.
- A forecast job that fails to load weights must fail visibly; never return dummy paths.
- Keep input candle fingerprints and model revisions with forecast outputs.
- Use a retention policy for old candle snapshots, forecasts and event logs; export reports before pruning.
- Make DB backups and test restore. Never use an attached per-service disk as shared storage.
- Do not mark a system signal as live-executed. Every generated candidate is research/paper-only.


## Database migrations

Run `python -m app.migration_lock` locally or let the Render containers run it during startup. On PostgreSQL it obtains a session-level advisory lock so concurrent web/worker deployments do not race to apply the same Alembic migration.
