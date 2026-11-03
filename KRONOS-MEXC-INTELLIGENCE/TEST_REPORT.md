# KRONOS-MEXC-INTELLIGENCE — Build Verification Report

**Snapshot date:** 2026-11-03  
**Project version:** 1.0.0  
**Operating mode:** research and paper only; no live order execution.

## Checks completed in this execution environment

| Check | Result | Evidence / scope |
|---|---|---|
| Backend automated tests | PASS | `PYTHONPATH=. pytest -q` — 32 passed |
| Python byte compilation | PASS | `python -m compileall -q app scripts alembic` |
| Editable package install | PASS | `pip install -e . --no-deps --no-build-isolation` |
| Alembic fresh SQLite migration | PASS | `python -m app.migration_lock`; `0001_initial (head)`, using a fresh SQLite file |
| Render Blueprint syntax | PASS | `yaml.safe_load`; 3 services and 1 database parsed |
| Migration startup lock | PASS | `python -m app.migration_lock` completed a fresh SQLite migration; PostgreSQL advisory-lock path is implemented but requires a live PostgreSQL test |
| FastAPI health route | PASS | TestClient `/healthz` returned 200 |
| Login and authenticated status | PASS | TestClient login; `/api/status` returned research-only mode |
| WhatsApp signal guard | PASS | API status reports `BLOCKED_FOR_CRYPTO_TRADING_SIGNALS`; no send implementation |
| Frontend TS/TSX syntax | PASS | Global TypeScript transpiler checked 4 `.ts`/`.tsx` source files; no syntax diagnostics |

## Not verified here — do not interpret as passing

| Check | Status | Reason / next verification |
|---|---|---|
| Full frontend npm build | NOT RUN | npm package installation could not reach the npm registry in this environment. GitHub Actions runs install/build on a network-enabled runner. |
| Docker image builds | NOT RUN | Docker CLI/daemon is not installed in this environment. |
| Live MEXC REST/WebSocket integration | NOT RUN | Outbound network access is unavailable in this execution environment. Use deployment health checks and compare payloads with current official docs. |
| Kronos weights download and actual inference | NOT RUN | GitHub/Hugging Face downloads require outbound network. Source and model/tokenizer revisions are pinned; the inference adapter is present but must be benchmarked on the target compute service. |
| Render deployment | NOT DEPLOYED | No Render account authorization or deployment action is available in this execution. |
| GitHub private repository creation/push | NOT CREATED | The available GitHub connection supports repository read and file actions but no repository-creation action was available. The complete source is packaged for the owner to push. |
| WhatsApp crypto signal delivery | DISABLED BY DESIGN | Meta Business Messaging Policy restricts promoting/facilitating virtual-currency exchange. This application does not send these messages. |
| Ruff / mypy | NOT RUN | Their command-line executables are not installed in this environment. CI config includes backend lint/test steps. |

## Known functional limitations

1. `BACKTEST ALL` runs independent equal-equity sleeves per symbol. It is not a synchronized, cross-symbol, cross-margin portfolio simulation and does not model portfolio-level correlation or exchange liquidation across positions.
2. Backtests use current contract metadata where archived point-in-time metadata is unavailable, and cannot recreate historic order-book queue priority, market impact, actual partial fills or full MEXC cross-margin liquidation mechanics.
3. Kronos path shares are model sample frequencies, not calibrated market probabilities. Forecast evaluation must accumulate untouched out-of-sample observations before making calibration claims.
4. Kronos-filtered backtests can be computationally expensive because inference is performed at historical decision points. No performance claim is made before target-hardware benchmarking.
5. Signal candidate creation, fingerprints, cooldown/expiry and active-paper risk caps exist. Automated subsequent candle-based signal outcome reconciliation and a realized daily-loss kill switch are not implemented.
6. Forecast arrays are gzip-compressed in PostgreSQL. An S3-compatible artifact backend and artifact-retention/compaction task are not implemented.
7. The paper simulator is research logic only; it does not reproduce actual exchange fills or manage a real account.
8. WhatsApp crypto-futures signal delivery is hard-blocked, not merely disabled by configuration.

## Deployment gate

Before describing this as operational, complete all of the following on network-enabled infrastructure:

- Build frontend and all three Docker images.
- Deploy the Blueprint and verify web/worker services share the same PostgreSQL database.
- Confirm live MEXC payload parsing and WebSocket reconnect behaviour.
- Download the pinned Kronos source/weights, run the real inference smoke test, and benchmark 1,000 paths on the selected CPU plan.
- Run a real historical data download, inspect missing-data warnings and execute a walk-forward backtest.
- Keep WhatsApp crypto-signal delivery disabled.

A passing offline suite is not evidence of profitability or production readiness.
