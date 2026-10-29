# Test Report — MEXC RSI/EMA Bot v0.4.0

Date: 29 October 2026

## Results

| Check | Result |
|---|---|
| Python compilation | PASS — `python -m compileall -q app tests` |
| Automated tests | PASS — `pytest -q`, 94 passed |
| Render Blueprint YAML | PASS — one web service, one background worker, one PostgreSQL database parsed |
| Local web smoke test | PASS — `/healthz` and `/dashboard` both returned HTTP 200 with application lifespan enabled |
| Live flags | PASS — `live_execution_enabled=false`, `live_execution_build_released=false` |
| Ruff lint | NOT RUN — Ruff executable unavailable in this environment |
| MEXC public API reachability | BLOCKED — DNS resolution failed in this runtime |
| Real historical performance backtest | NOT RUN — no verified MEXC historical candles were downloaded; no performance numbers are claimed |
| Live MEXC order test | NOT RUN — no released private order path and no order was submitted |
| WhatsApp production delivery | NOT RUN — no live Meta credentials or approved crypto notification path tested |
| PostgreSQL / Render deploy | NOT RUN — no cloud resources created |

## What the automated tests cover

The suite includes candle interval aggregation, RSI/EMA signal rules, completed-candle timing, no future data use in the deterministic strategy tests, contract-sized paper sizing, fee and slippage calculations, conservative stop/target/liquidation priority, empty-data backtest labeling, chart output with multiple timeframes, command policy checks, webhook/authentication behavior, storage and risk regression cases.

## Interpretation

The 92 passing tests establish regression evidence for the tested code and fixtures only. They do not prove future profitability, correct behavior against every live MEXC payload, live order correctness, or production availability. Strategy fixtures and any synthetic demonstration data are not market-performance evidence.

The new live-market backtest must be run from a network environment that can reach MEXC. Its JSON must be inspected for symbol coverage, missing candles, missing funding intervals, contract metadata validation and the assumptions applied to costs and liquidation before interpreting any performance value.
