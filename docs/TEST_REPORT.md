# Automated Test Report — v0.8.4

## Local verification

- `pytest -q`: **174 passed**.
- `python -m compileall -q app tests`: passed.
- Application/worker imports: passed as part of the test and import checks.
- Render Blueprint YAML parse: passed; both services point to the same PostgreSQL database and the background worker uses `python -m app.worker`.
- Targeted regressions cover active Cross Margin portfolio sizing, 50-position cap, liquidation stress proxy, open-equity reconciliation, exit-aware MAE/MFE, liquidation event timestamps, and final-run funding coverage.
- `git diff --check`: passed.

## Runtime environment

- Local verification ran on Python 3.13.5. The Dockerfile targets Python 3.12; Python 3.12 was not available for a second local run.
- Ruff was not installed in this environment, so linting is **not verified** here. The GitHub Actions lint job should run before release.

## Verification boundaries

These checks validate source-level behavior and deterministic fixtures only. They do **not** prove that Render has deployed the same commit to both services, that production PostgreSQL credentials match, that the worker can reach MEXC, that a real historical backtest completes, that Meta approves crypto messaging, or that a strategy is profitable. Those require post-deployment checks.

The active S1-RFPC backtest uses completed 1D/12H/4H/1H candles and MEXC REST history. It does not activate the legacy 3M RSI/EMA or MTF-TPR strategy modules. It also does not reproduce the exchange-exact Cross Margin liquidation process: liquidation is a conservative 1H trade-price OHLC stress proxy, without historical Fair Price, external positions/open orders, partial liquidation, ADL or order-book impact.
