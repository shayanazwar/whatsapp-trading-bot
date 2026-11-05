# Automated Test Report — v0.8.6

## Local verification

- `pytest -q`: **180 passed**.
- `python -m compileall -q app`: passed.
- Render Blueprint YAML parse: covered by regression tests; one web service only, with an in-process queue consumer and scanner scheduler enabled.
- Queue regressions cover command enqueue, same-process claim/execute/complete, one-at-a-time execution, heartbeat visibility, restart recovery and requeue after cancellation.
- Existing strategy/backtest, data validation, portfolio accounting, storage, command and webhook suites remain passing.

## Runtime architecture

The web application starts a queue-only backtest consumer in its existing process. A PostgreSQL advisory lock is shared with the legacy standalone worker entry point to prevent two active consumers. The Render Blueprint no longer declares a separate worker service.

## Verification boundaries

These checks validate source-level behavior and deterministic fixtures only. They do **not** prove that Render has deployed this commit, that production credentials/database connectivity are valid, that MEXC historical data is currently available, or that a live network backtest completes. After deployment, confirm `/healthz` reports `in_process_backtest_queue=true`, then run `BACKTEST 1D` and inspect web-service logs and `BACKTEST STATUS`.

The simulator remains a research estimate using completed 1D/12H/4H/1H candles, modeled costs and observed funding history. It does not reproduce the exchange-exact order book, external positions, all partial fills or cross-margin liquidation engine. Live order execution remains hard-locked off.
