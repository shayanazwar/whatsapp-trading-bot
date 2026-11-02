# Final Source Audit — v0.8.1

## Result

This is a complete repository archive intended to replace the source files in the GitHub repository root. It is not a replacement-only patch. The archive keeps the GitHub Actions workflow, application, tests, Dockerfile, Render Blueprint, lock file, example environment and operating documentation.

## Verified locally

- Automated tests: 142 passed.
- Python byte-compilation: passed.
- Runtime imports: `app.main`, `app.worker`, `app.services.scheduler`, `app.services.command_processor`, `app.analysis` and `app.backtest` passed.
- Wheel packaging: succeeded (`mexc_futures_whatsapp_research_bot-0.8.1-py3-none-any.whl`).
- Render Blueprint YAML: parsed successfully; both web and worker share the same Render PostgreSQL connection-string reference; worker command is `python -m app.worker`; the queue poll interval is 5 seconds; no legacy plan overrides remain.

## Architecture

- Active `BACKTEST`, `SCAN` and `ANALYZE` use S1-RFPC and completed 1D → 12H → 4H → 1H candles.
- `BACKTEST` is queued by the web service and claimed by the separate `app.worker` process.
- Queue polling is independent from the periodic market scanner and defaults to 5 seconds.
- Worker startup, queue readiness, job claim, `BACKTEST START`, universe load and symbol progress have explicit logs.
- Backtest research positions are capped at 50 subject to risk and portfolio gates; paper concurrency remains 1. Live execution remains disabled/unavailable.
- The old 3M RSI/EMA code remains as inactive historical code but is not imported by active command dispatch or used by active BACKTEST/SCAN routes.

## Fundamentals finding

No tokenomics/on-chain fundamentals provider exists in this project. The release does not calculate market capitalization, FDV, supply, unlocks/vesting, on-chain activity, protocol revenue, development activity, governance or news scores. `ANALYZE` explicitly reports `fundamentals.status=NOT_IMPLEMENTED`. Funding rate, open interest, contract metadata, turnover and order-book depth are derivatives/market data and are not presented as token fundamentals.

## Deployment boundary

A passing local suite cannot prove that Render deployed the same commit to both services, that their production database URLs match, that the live MEXC API responds, or that Meta accepts a WhatsApp report. After pushing, verify the worker startup/heartbeat and confirm one queued job is actually claimed and scanned. Do not delete the existing Render PostgreSQL database. Keep the WhatsApp crypto-message gate closed unless actual written clearance and recipient authorization are configured.

## Research limitations

The simulator is not a perfect exchange replay. It cannot guarantee 100% accuracy and does not fully replay historical order-book queue priority, full market impact, external open positions/orders, point-in-time universe composition or the exchange's complete cross-margin liquidation engine.

## Lint note

Ruff is declared in the project's test extra and GitHub Actions workflow, but the `ruff` executable was not installed in the audit environment. The GitHub Actions lint job remains the final automated lint check; no claim is made that Ruff has passed locally.
