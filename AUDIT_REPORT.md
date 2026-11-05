# RSLR Trading Bot — Build & Forensic Audit

**Audit date:** 2026-11-06  
**Build state:** Local source implementation completed; external service deployment not completed.

## Verified in this build

| Check | Result |
|---|---|
| Python compilation (`compileall`) | PASS |
| Automated test suite | PASS — 29 tests |
| Fresh SQLite Alembic migration | PASS |
| Notification schema includes external WhatsApp message ID | PASS |
| Editable package installation (`pip install -e . --no-build-isolation --no-deps`) | PASS |
| CLI entry point (`rslr --help`) | PASS |
| Render Blueprint YAML syntax | PASS |
| Docker Compose YAML syntax | PASS |
| GitHub Actions YAML syntax | PASS |
| Live MEXC REST call | NOT VERIFIED in this runtime |
| Live WhatsApp webhook / message send | NOT VERIFIED; Meta credentials unavailable |
| Render deployment | NOT DEPLOYED |
| Live trading | HARD-LOCKED OFF |
| Historical 1D / 7D / 30D performance run | NOT RUN against live MEXC data; no performance values are claimed |

## Implemented modules

- MEXC USDT-M public REST market-data client, contract metadata, ticker universe and funding-history retrieval.
- UTC candle validation and aggregation: 1H source, 4H from four contiguous 1H bars, 12H from exactly three contiguous 4H bars, and 1D from 24 contiguous 1H bars.
- BTC daily regime, BTC-relative 12H strength ranking, 4H context, historical funding context, Sweep/Reclaim and Donchian modules.
- Contract precision-aware sizing, 0.5% equity risk target including modeled costs, one gross 1R target, margin/leverage restrictions and minimum contract-size rejection.
- Chronological 1D/7D/30D backtest runner, next-hour-open entry, funding/fee/slippage accounting, conservative same-bar stop-first logic, stop-gap handling, portfolio caps and unresolved-position classification.
- Generated-signal counts are separate from simulated trades entered.
- 30D 70/30 chronological walk-forward reporting; no out-of-sample parameter optimization.
- SQLite/PostgreSQL storage models, Alembic initial schema, signal/backtest persistence, paper outcomes, notification outbox, deduplication, message receipt correlation and database scheduler locks.
- FastAPI health and WhatsApp webhook endpoints, HMAC verification, sender allowlist, command router, PNG charts and proactive template send support.
- Paper tracking is configurable. Live execution submission is blocked by immutable source-level safety constants.
- Docker, Docker Compose, Render Blueprint and GitHub CI files.

## Critical limitations

1. **No live market-data test was run.** MEXC adapter behavior is covered by mocked responses and implemented against the documented public Futures API. A real connection must be verified after deployment. MEXC documents a 2,000-record maximum for kline responses; requested 42-day hourly warm-up remains below that cap under a complete data series. Historical gaps are not synthesized.
2. **No actual performance statistics exist yet.** Backtest commands are implemented, but they were not executed against MEXC during this build. Do not treat fixture tests or illustrative WhatsApp formatting as results.
3. **Universe survivorship bias remains.** The implementation selects the currently eligible/currently liquid contract universe; it does not reconstruct every historical listing/delisting and past liquidity rank.
4. **Execution simulation is bar-based.** It cannot establish true intrabar path, queue position, market impact or depth. If SL and TP are both touched in one 1H bar, SL is assumed first.
5. **Historical OI is not fabricated.** Current ticker OI snapshots are stored when provided by MEXC; no historical OI gate is used.
6. **Live order execution is not implemented.** The code prepares an order description only. `submit_order()` always raises `ExecutionLocked`; no authenticated order-transport method exists. This must remain disabled until a separately audited implementation is deliberately authorized.
7. **WhatsApp and Render are not externally configured.** Owner-supplied Meta credentials, sender allowlist, approved proactive template and Render service/database are required. Proactive sends use template messages; message status callbacks update stored receipts when Meta sends them.
8. **Dependency lock file was not generated.** The runtime had no package-index access for full transitive resolution. `pyproject.toml` is the authoritative dependency range specification. A real lock should be generated in CI or a network-enabled build environment rather than fabricated.
9. **GitHub repository was not created.** The connected GitHub account exposed no repositories and the connected GitHub actions do not include repository creation. The project can be pushed after a blank `RSLR_TRADING_BOT` repository is created.

## Safety conclusion

The delivered code is a signal-generation and historical/paper-trading implementation, not a production-authorized live trading system. No live orders can be submitted through this build. No profitability, accuracy or positive-expectancy claim is made. Complete external setup, run MEXC historical backtests, inspect raw trade logs, and evaluate out-of-sample behavior before considering any further execution work.
