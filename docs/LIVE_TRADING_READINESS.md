# Live Trading Readiness — v0.8.1

## Verdict: NOT READY / NOT RELEASED

This repository is a research and paper-simulation codebase. No live MEXC order-placement flow is released. The `MexcClient` contains public market-data GET endpoints only. API key/secret settings are legacy placeholders and are not used to submit orders.

Live trading must remain off: `AUTO_TRADING_ENABLED=false`, `LIVE_EXECUTION_ENABLED=false`, `LIVE_EXECUTION_BUILD_RELEASED=false`. Paper mode also remains off until the separate data-alignment probe and price-series review are complete.

Before any future live implementation, a separate safety review must cover signed request construction, client-order idempotency, ambiguous POST timeout reconciliation, reduce-only/position mode, leverage and margin state, contract tick/volume precision, liquidation/maintenance-margin calculations, exchange-side order state reconciliation, kill switches, rate limits, testnet/paper validation and operator monitoring. Do not infer readiness from the existence of API credentials.

Backtest TP/SL outcomes use historical OHLC and modeled execution costs. They do not guarantee fills, market impact, queue priority, funding availability or full cross-margin liquidation behavior.
