# Known limitations

- The actual Kronos model source and pretrained weights are not bundled; install/download scripts use pinned revisions and require a network-enabled build.
- Live MEXC REST/WebSocket exchange behaviour must be verified after deployment. Public API schemas/rate limits can change.
- MEXC does not provide a futures sandbox. Historical backtesting and paper signals are local simulations.
- Current contract metadata may be used for past backtest periods when archived metadata is unavailable.
- The engine does not reproduce historical order-book queue position, full order-book market impact, actual partial fills, external account positions or the complete exchange cross-margin liquidation engine.
- WhatsApp crypto-futures signal delivery is hard-blocked due to Meta policy.
- Render CPU inference performance is unverified until a real service benchmark is run.
- The backtester evaluates one symbol per job (or equal independent sleeves for `ALL`); `ALL` is not a synchronized cross-symbol portfolio replay and does not model portfolio-level correlation/margin interactions.
- Kronos-filtered backtests call the inference adapter per eligible decision; long periods can be computationally expensive. The baseline comparison suite is documented as research methodology but is not yet an automated multi-model comparison report.
- Signal creation, deduplication, expiry and paper-risk checks are implemented; automated signal outcome reconciliation against subsequent live candles and a realized daily-loss kill switch are not implemented.
- Forecast artifacts are gzip-compressed inside PostgreSQL. There is no S3-compatible artifact backend or retention/compaction job in this release.
- The frontend bundle has not been built in this environment because npm package installation could not reach the registry. CI performs the frontend install/build after a network-enabled GitHub runner starts.
- A green unit-test result does not prove profitable performance or production readiness.
