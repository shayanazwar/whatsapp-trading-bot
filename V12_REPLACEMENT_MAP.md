# V12 file replacement map

| Old path | New path | Why it changed |
|---|---|---|
| `.dockerignore` | `.dockerignore` | Prevents the ignore rule from excluding `app/artifacts/` from Docker context. |
| `.gitignore` | `.gitignore` | Restricts the local artifact-cache ignore rule to the repository root so the required Python package can be committed. |
| `.env.example` | `.env.example` | Documents canonical `META_*` WhatsApp credential names. |
| `render.yaml` | `render.yaml` | Shares canonical Meta sender credentials with worker services and keeps role-specific workers. |
| `pyproject.toml` | `pyproject.toml` | Release version set to 1.2.0; dependency pins retained. |
| `app/config.py` | `app/config.py` | Prioritizes `META_*` environment variables over legacy `WHATSAPP_*` aliases. |
| `app/models.py` | `app/models.py` | Adds persisted `Job.next_attempt_at` retry scheduling. |
| *(missing in submitted root ZIP)* | `app/artifacts/__init__.py` | Restores the artifact package imported by the worker pipeline. |
| *(missing in submitted root ZIP)* | `app/artifacts/store.py` | Restores database-backed compressed forecast path storage. |
| *(new)* | `alembic/versions/0003_job_retry_schedule.py` | Adds retry schedule column and index idempotently. |
| `app/services/jobs.py` | `app/services/jobs.py` | Role-aware claiming, backoff retry, stale lease recovery, and worker-owner-safe state transitions. |
| `app/worker.py` | `app/worker.py` | Logs boot/import failures, traces job/request IDs, renews owner-bound leases, and records explicit job lifecycle events. |
| `app/services/pipeline.py` | `app/services/pipeline.py` | Adds job/analysis/backtest/Kronos trace logs, real forecast summary metadata and forecast baseline evaluation. |
| `app/api/routes.py` | `app/api/routes.py` | Adds queue-age/count and market/worker/failure diagnostics to authenticated status. |
| `app/main.py` | `app/main.py` | Separates process liveness from cheap database readiness; app version 1.2.0. |
| `app/notifications/whatsapp_commands.py` | `app/notifications/whatsapp_commands.py` | Adds job ownership status search, queue/worker/health diagnostics, structured request logs, and optional Kronos backtest command. |
| `app/forecasting/kronos.py` | `app/forecasting/kronos.py` | Adds path-derived direction/volatility/median OHLC summaries and input-only drift baseline metadata. |
| `app/forecasting/evaluation.py` | `app/forecasting/evaluation.py` | Compares matured Kronos forecasts with naive and simple drift baselines. |
| `app/backtesting/engine.py` | `app/backtesting/engine.py` | Adds full result/rejection/regime metrics; safely labels missing regime as UNKNOWN. |
| `frontend/package.json` | `frontend/package.json` | Frontend release version set to 1.2.0. |
| `frontend/src/App.tsx` | `frontend/src/App.tsx` | Adds the Kronos next-direction, expected-price, path-share, volatility and uncertainty panel. |
| `frontend/src/components/ForecastChart.tsx` | `frontend/src/components/ForecastChart.tsx` | Draws distinct model-generated median OHLC candles, bands, paths and validated level overlays. |
| `docs/RENDER_DEPLOYMENT.md` | `docs/RENDER_DEPLOYMENT.md` | Documents four-process role separation and canonical Meta variables. |
| `README.md` | `README.md` | Updates release capabilities, commands, worker requirements and limitations. |
| `tests/test_forensic_regressions.py` | `tests/test_forensic_regressions.py` | Adds environment precedence and ignore-file regression coverage. |
| `tests/test_jobs.py` | `tests/test_jobs.py` | Adds retry, owner isolation, import and worker consume/finish lifecycle tests. |
| `tests/test_whatsapp_integration.py` | `tests/test_whatsapp_integration.py` | Uses deterministic mocks and verifies optional Kronos backtest command routing. |
| *(new)* | `tests/test_kronos_summary.py` | Tests forecast-path summary validation and median OHLC calculations. |
| *(new)* | `tests/test_forecast_evaluation.py` | Tests Kronos metrics against last-value and drift baselines. |
| *(new)* | `V12_AUDIT_REPORT.md` | Documents evidence, root causes, architecture, repairs and remaining limitations. |
| *(new)* | `V12_TEST_REPORT.md` | Records exact local checks and untestable external integrations. |
| *(new)* | `V12_DEPLOYMENT.md` | Provides replacement, Git, Render and rollback steps. |
| *(new)* | `V12_REPLACEMENT_MAP.md` | This file map. |
| *(new)* | `MANIFEST_SHA256.txt` | SHA-256 checksums for all packaged files except the manifest itself. |
