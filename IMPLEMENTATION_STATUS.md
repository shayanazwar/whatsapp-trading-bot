# Implementation Status — v0.8.1

## Implemented

- Active S1-RFPC analysis/backtest/scanner strategy on completed 1D → 12H → 4H → 1H bars.
- Deterministic 12H aggregation from completed UTC-aligned 4H candles.
- Durable PostgreSQL backtest job queue, worker claim/recovery, persisted progress and reports.
- Independent backtest queue poll loop (default 5-second polling) running concurrently with market scanning.
- Worker heartbeat/build/database fingerprint and `/healthz` worker-vs-scanner diagnostics.
- Explicit worker and symbol progress logs, including an early `BACKTEST START` log before external data preparation.
- Active scanner uses shared market precision utilities and does not import the legacy 3M RSI/EMA strategy code.
- Render Blueprint avoids legacy hard-coded compute plan identifiers and preserves existing resource plans.
- Research backtest maximum concurrent positions configurable up to 50; separate paper simulation remains at one.
- TP/SL and cost reporting with documented simulation limitations.
- Explicit `ANALYZE` fundamentals status: `NOT_IMPLEMENTED` until a verified tokenomics/on-chain provider is integrated.

## Not implemented / not proven

- Token fundamentals data provider, historical tokenomics snapshots, unlock data, on-chain metrics or fundamentals scoring.
- Historical order-book replay, queue priority, full partial fills/market impact or complete exchange cross-margin liquidation.
- Live MEXC order submission/reconciliation.
- Production verification of Render deployment, PostgreSQL sharing, external MEXC data availability or Meta WhatsApp message delivery.

## Safety defaults

Paper trading, auto trading and live execution remain disabled. Crypto WhatsApp signals/reports remain behind the separate policy-clearance gate. The repository must not be described as a live-trading-ready system or a fully fundamental-analysis system.
