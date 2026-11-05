# Forensic Repository Audit — v0.8.1

## Scope

Source-level review of the complete repository covering command dispatch, backtest simulation, worker lifecycle, queue persistence, Render topology, MEXC public market-data client, technical analysis and fundamentals coverage.

## Confirmed architecture

- WhatsApp/web handler parses `BACKTEST`, persists a durable job and responds immediately.
- Separate `app.worker` process claims jobs from shared PostgreSQL.
- The active backtest route is `_run_backtest` using S1-RFPC completed 1D/12H/4H/1H.
- The active scanner also evaluates S1-RFPC on 1D/12H/4H/1H.
- The 3M RSI/EMA implementation remains unused legacy code and is not dispatched by active `BACKTEST`/`SCAN` routes.

## Defects and corrective actions in v0.8.1

1. Stale `.env.example` claimed 3M RSI/EMA was the active strategy; replaced with aligned v0.8.1 settings and safe local defaults.
2. Queue polling shared the market-scan interval; separated `BACKTEST_JOB_POLL_SECONDS` (default 5 seconds) from the scanner cycle.
3. Health diagnostics could label scanner disabled without describing worker health; now reports `worker_health` independently and can compare non-secret database fingerprints.
4. Backtest logs only began after initial time/universe work; added `BACKTEST START` and explicit time/universe preparation logs.
5. Queue claim database errors could exit the poll task without a clear queue-specific line; now log `BACKTEST QUEUE CLAIM FAILED` and retry on the next poll.
6. `ANALYZE` did not disclose that token fundamentals were absent; now returns and formats `fundamentals.status=NOT_IMPLEMENTED`.
7. Ticker throttling spacing was tightened to a conservative 0.21s interval.
8. The active scanner depended on a rounding helper inside the legacy RSI/EMA module; moved the helper into `app.market.precision` and retained a compatibility alias for the standalone legacy backtest.
9. The queue poller and market-scan loop were still serialized in one scheduler coroutine; split them into independent asyncio tasks so a slow scanner/API cycle cannot block durable backtest job claiming. Added a regression test for this.
10. Removed the unreachable legacy RSI/EMA backtest method and legacy strategy imports from the active command processor, preventing old 3M code from being imported as part of active app startup.
11. Removed outdated explicit Render `plan` IDs from `render.yaml`; existing resource plans are retained and new resources use Render defaults.

## Fundamental-data verdict

There is no tokenomics/on-chain fundamentals provider in this repository. Funding and OI are derivatives data; contract metadata, tickers and depth are market data. No market cap, circulating/total/max supply, FDV, unlock/vesting, on-chain activity, protocol revenue, developer activity or governance/news score is computed. This is documented and surfaced in `ANALYZE`; no synthetic fundamental values are created.

## Known limitations

Backtest fills/costs are modeled from completed OHLC bars and available funding events. No historical order-book queue replay, full market impact/partial fills, external account positions/open orders, point-in-time historical top-100 membership or complete exchange cross-margin liquidation engine is included. This is not a 100% accurate exchange replay and does not establish profitable performance.

## Deployment still requiring external verification

The code cannot verify its own Render deployment. Operators must confirm the web and worker services use the same Git commit and PostgreSQL `DATABASE_URL`, worker startup/heartbeat are healthy, and a queued job is actually claimed and progressed in worker logs. Meta policy gate, recipients and credentials are separate WhatsApp delivery prerequisites.
