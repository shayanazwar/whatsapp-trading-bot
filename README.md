# PAK TRADING ACADEMY — MEXC Futures Research Platform v0.8.6

Research-only MEXC USDT perpetual scanner, swing-strategy backtester, in-process durable job queue and protected dashboard. v0.8.6 runs the durable backtest consumer inside the existing web service, without a separate Render worker. v0.8.5 enables all supported allowlisted WhatsApp commands and removes the separate research-message gate. v0.8.4 includes audited fixes on top of v0.8.3, including exit-aware MAE/MFE and final-run funding coverage. v0.8.3 added bounded per-position initial-margin sizing, contract leverage/MMR checks, a shared-wallet maintenance stress proxy, marked-to-market open positions, stronger ledger reconciliation and memory telemetry. It preserves the active S1-RFPC 1D → 12H → 4H → 1H strategy. Live order execution is disabled and no live order-placement loop is available in this build.

## Strategy and supported timeframes

The active strategy is **S1-RFPC** and is restricted to completed candles on exactly these timeframes:

`1D regime → 12H structure → 4H setup → 1H entry trigger`

The 12H series is deterministically aggregated from completed UTC-aligned 4H bars. The active swing scanner and the active backtest command use the same strategy and timeframe sequence. The previous 3M RSI/EMA backtest path is not used by `BACKTEST` or `SCAN`; its code remains only as inactive legacy implementation for historical comparison.

## Backtest and portfolio model

- `BACKTEST 1D` and `BACKTEST 7D` rank the current top 100 active USDT-settled MEXC perpetual contracts by 24-hour turnover. A symbol-specific command such as `BACKTEST BTCUSDT 30D` is supported for longer windows.
- Jobs are durably queued and claimed by a queue consumer running inside the existing Render web process through the shared PostgreSQL database. The consumer runs one backtest at a time, stores symbol-by-symbol progress and persists the completed report. The report is also placed in the outbox for the original allowlisted sender.
- Historical data is fetched with a bounded concurrency of five symbols for a backtest. Web-service logs include `BACKTEST SYMBOL SCAN | progress=1/100 ...`, plus per-symbol start, completion and failure details. The scheduled top-100 scan is deferred while a backtest job is using MEXC API capacity.
- The research portfolio cap is configurable from 1 to **50 simultaneous positions** (`BACKTEST_MAX_POSITIONS`, default 50). Per-entry modeled risk defaults to 0.5% of realized balance; aggregate planned stop-risk is capped at 25%; gross notional is capped at 25× current modeled equity by default (configurable). These filters mean 50 is a ceiling, not a promise that 50 positions can be opened.
- The separate paper-trading simulator remains limited to one active position (`MAX_CONCURRENT_POSITIONS=1`). The research portfolio cap does not change paper or live execution settings.
- Reports show requested UTC window, symbols tested/eligible, 1H bars, strategy evaluations, candidates, entry attempts, maximum concurrent positions, open positions, TP hits, SL hits, time-stop exits, data-gap exits, fees, funding where available, slippage, drawdown, cost-stress runs and warnings.

## Accuracy and limitations

No historical backtest can be guaranteed 100% accurate. This simulator uses completed OHLC candles and modeled entry/exit fills, fees, slippage and available funding history. It does not replay historical order-book queue position or impact, all partial-fill behavior, the user's external positions/open orders, or exchange-exact cross-margin maintenance-margin and liquidation mechanics. The active simulator reports a conservative 1H trade-price OHLC maintenance stress proxy; this is not an exchange-exact liquidation replay. Positions still open at the end of a test are separately marked to market and included in ending equity, but not in realized P&L or closed-trade win-rate statistics. Current top-100 membership also introduces survivorship/selection bias for historical periods. Treat results as research estimates, not confirmed exchange execution.

The backtest is chronological. Entries at the same timestamp are allocated in symbol order for deterministic results. Data fetch failures, insufficient history, unverified contract metadata and missing funding are surfaced as warnings/rejections; missing data is not filled with fabricated candles or prices.

## Fundamental analysis status

`ANALYZE` provides technical/structure analysis only. Tokenomics and on-chain fundamentals are **not implemented** in this build: no market-cap, supply, FDV, unlock, on-chain activity, protocol revenue, developer activity or governance/news provider is configured. The analysis output explicitly returns `fundamentals.status=NOT_IMPLEMENTED`. Funding rates are derivatives carry data, not token fundamentals. See `docs/FUNDAMENTALS_DATA_AUDIT.md`.

## WhatsApp signals and reports

The single Render web service runs the top-universe swing scanner once per completed 1H candle (`SCHEDULER_ENABLED=true` in the supplied blueprint). A signal is sent only when S1-RFPC produces a candidate and the delivery checks pass: valid contract/price tick, sensible rounded stop/target geometry, minimum net reward/risk after modeled fees/slippage, usable top-of-book depth/spread, and an available non-adverse current funding snapshot. The system does **not** broadcast rejected or incomplete candidates as trade signals.

All parsed WhatsApp commands are enabled for explicitly configured senders in `WHATSAPP_ALLOWED_SENDERS`. The same allowlist protects inbound command execution and outbound replies/notifications. A valid webhook verification token, app-secret signature, access token and phone-number ID are required. Message de-duplication and the configurable monthly outbound cap remain active.

The user-facing `BACKTEST` acknowledgement is sent immediately after durable queueing. The same Render web service claims and runs the job in the background; a separate paid worker service is not required. Scanner signals are only pushed when the strategy's own data-quality, liquidity, contract and reward/risk checks pass.

## Commands

```text
BACKTEST 1D
BACKTEST 7D
BACKTEST BTCUSDT 30D
BACKTEST STATUS
BACKTEST REPORT <JOB_ID>
SCAN
SIGNALS
STATUS
HELP
```

Legacy `CONTROL`, `CANDIDATE`, `BOTH`, `LEGACY`, and `FIXED` backtest selectors are rejected so the same command cannot silently run the old 3M model.

## Architecture

```text
MEXC Futures public API
        |
        v
Single Render web service (one Uvicorn worker)
  - signed inbound webhook + protected dashboard
  - durable BACKTEST enqueue + in-process queue consumer
  - optional top-100 S1-RFPC scanner and gated paper scheduler
  - status, progress, reports and /healthz
        |
        v
Shared PostgreSQL database
  - inbound/outbound idempotency
  - backtest jobs, progress and reports
  - scanner state, events and outbox
  - process heartbeat and advisory-lock coordination
```

The supplied Render Blueprint defines one web service and the shared PostgreSQL database. The web process runs one backtest at a time in its own background task; `BACKTEST_JOB_POLL_SECONDS` controls queue polling independently of the market-scanner interval. The PostgreSQL advisory lock prevents an old standalone worker or a second web instance from consuming the same queue. Stop/delete any previously deployed `mexc-rsi-ema-scanner` worker service before enabling this build. The backtest queue remains active even when `SCHEDULER_ENABLED=false`; that flag controls only the optional scanner/paper scheduler.

## Deploy and verify

1. Apply the replacement files and deploy the new commit to `mexc-rsi-ema-web`.
2. Stop/delete the old `mexc-rsi-ema-scanner` background worker if it still exists; deploy only one web instance with one Uvicorn worker.
3. Confirm `/healthz` reports `in_process_backtest_queue=true`, `backtest_executor_status=running`, a fresh `worker_heartbeat_age_seconds`, and `worker_database_match=true`.
4. Run `BACKTEST 1D`; web-service logs should show `IN-PROCESS BACKTEST QUEUE READY`, `BACKTEST JOB START`, `BACKTEST START`, `BACKTEST SYMBOL SCAN | progress=1/N`, and `BACKTEST JOB COMPLETE`.
5. Use `BACKTEST STATUS` and `BACKTEST REPORT <JOB_ID>` to inspect progress and the persisted result. Keep all live-execution settings false.

## Local development

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\Activate.ps1
pip install -e '.[test]'
cp .env.example .env                      # Windows: copy .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For a single local process, blank `DATABASE_URL` uses SQLite; the in-process queue consumer starts automatically, so no separate worker command is needed. Set `WEB_API_TOKEN` before using dashboard research commands. Never commit `.env`, API tokens, Meta secrets or private account data.

## Validation

Run tests with `pytest -q`. Automated tests validate deterministic fixtures, parser behavior, job idempotency, data handling and simulator accounting. They do not prove current MEXC API availability, Render deployment health, WhatsApp account eligibility or future strategy profitability.


## Release notes

See [`docs/RELEASE_NOTES_v0.8.6.md`](docs/RELEASE_NOTES_v0.8.6.md) for the in-process backtest queue fix and [`docs/RELEASE_NOTES_v0.8.5.md`](docs/RELEASE_NOTES_v0.8.5.md) for the runtime enablement changes and [`docs/RELEASE_NOTES_v0.8.4.md`](docs/RELEASE_NOTES_v0.8.4.md) for the latest audit corrections and [`docs/RELEASE_NOTES_v0.8.3.md`](docs/RELEASE_NOTES_v0.8.3.md) for the portfolio-risk changes.
