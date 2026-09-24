# Project State — Pak Trading Academy WhatsApp Bot

Date: 2026-09-24

## What is in this package

The canonical runtime is the `app/` package. Market data, analysis, alerts, charts, signals, and future execution are MEXC Futures based. Alternate-exchange market paths have been removed from the production runtime. WhatsApp remains the interface/notification layer.

## Safety state

```text
SCANNER_ENABLED=false
AUTO_SIGNAL_ENABLED=false
AUTO_TRADE_ENABLED=false
ALLOW_LIVE_EXECUTION=false
```

These are the shipped defaults.

## Automation components

```text
app/automation/mexc_client.py
app/automation/universe.py
app/automation/scanner.py
app/automation/setup_filter.py
app/automation/signal_validator.py
app/automation/risk_manager.py
app/automation/signal_manager.py
app/automation/scheduler.py
app/automation/executor.py
```

The executor contains a verified limit-order payload builder and current MEXC endpoint adapter, but `LIVE_IMPLEMENTED = False` hard-stops real order placement. Post-fill position reconciliation and protective TP/SL installation still need to be completed and tested before live execution is enabled.

## Current MEXC API facts verified from official documentation

- Futures API base: `https://api.mexc.com`
- Public contract, ticker, kline, and server-time endpoints are available.
- Current Futures API supports order placement.
- Current API supports position-based TP/SL placement.
- Private Open-API authentication uses `ApiKey`, `Request-Time`, `Signature`, and optional `Recv-Window`; HMAC-SHA256 is used as documented.
- `Recv-Window` is treated as seconds and capped at 60 in this implementation; the default is 10.
- MEXC currently has no sandbox/test environment.

## Tests

Current local result before packaging:

```text
12 passed
```

The repository was also compiled with `python -m compileall -q .` successfully.

## Deployment note

The current design assumes one Render web service instance so that only one scanner loop is active. The scheduler is idempotent within a process.

Never commit `MEXC_SECRET_KEY`, `META_ACCESS_TOKEN`, or any other secret to GitHub.
