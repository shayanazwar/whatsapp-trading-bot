# Automated Test Report — v0.8.1

## Local verification

- `python -m pytest -q`: **142 passed**.
- `python -m compileall -q app tests`: passed.
- Runtime import smoke test: `app.main`, `app.worker`, `app.services.scheduler`, `app.services.command_processor`, `app.analysis`, and `app.backtest` imported successfully.
- Render Blueprint parsed as YAML; web and worker reference the same PostgreSQL connection string; worker command is `python -m app.worker`; both services inherit the 5-second backtest queue polling setting.
- No explicit Render plan overrides are present, avoiding legacy plan identifiers and preserving existing resource plans during a source-only update.

## Verification boundaries

These checks validate source-level behavior and deterministic fixtures only. They do **not** prove that Render has deployed the same commit to both services, that production PostgreSQL credentials match, that the worker can reach MEXC, that Meta approves a WhatsApp message, or that a strategy is profitable. Those require post-deployment checks in `docs/DEPLOYMENT_GUIDE_V0.8.1.md`.

`ruff` is declared in the test extra and the GitHub Actions workflow, but was not available in this execution environment; the lint job must run in GitHub Actions before treating lint as passed.

No token-fundamentals provider is implemented. `ANALYZE` explicitly reports fundamentals as `NOT_IMPLEMENTED`; funding, open interest, turnover, order-book depth and contract metadata are derivatives-market/context data, not a substitute for tokenomics or protocol fundamentals.

The backtest is a research simulation. It does not provide 100% accuracy and does not fully reproduce historical order-book queues, full market impact, external account positions/open orders, point-in-time universe membership, or exchange liquidation/margin behavior.
