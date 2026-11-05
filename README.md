# PAK TRADING ACADEMY — MEXC Futures Research Platform v0.8.3

Research-only MEXC USDT perpetual scanner, swing-strategy backtester, durable job worker and protected dashboard. v0.8.4 includes audited fixes on top of v0.8.3, including exit-aware MAE/MFE and final-run funding coverage. v0.8.3 added bounded per-position initial-margin sizing, contract leverage/MMR checks, a shared-wallet maintenance stress proxy, marked-to-market open positions, stronger ledger reconciliation and memory telemetry. It preserves the active S1-RFPC 1D → 12H → 4H → 1H strategy. Live order execution is disabled and no live order-placement loop is available in this build.

## Strategy and supported timeframes

The active strategy is **S1-RFPC** and is restricted to completed candles on exactly these timeframes:

`1D regime → 12H structure → 4H setup → 1H entry trigger`

The 12H series is deterministically aggregated from completed UTC-aligned 4H bars. The active swing scanner and the active backtest command use the same strategy and timeframe sequence. The previous 3M RSI/EMA backtest path is not used by `BACKTEST` or `SCAN`; its code remains only as inactive legacy implementation for historical comparison.

## Backtest and portfolio model

- `BACKTEST 1D` and `BACKTEST 7D` rank the current top 100 active USDT-settled MEXC perpetual contracts by 24-hour turnover. A symbol-specific command such as `BACKTEST BTCUSDT 30D` is supported for longer windows.
- Jobs are durably queued by the web process and claimed by the dedicated worker through the shared PostgreSQL database. The job stores symbol-by-symbol progress and the completed report. The report is also placed in the WhatsApp outbox when the crypto-message policy gate is open.
- Historical data is fetched with a bounded concurrency of five symbols for a backtest. Worker logs include `BACKTEST SYMBOL SCAN | progress=1/100 ...`, plus per-symbol start, completion and failure details. The scheduled top-100 scan is deferred while a backtest job is using MEXC API capacity.
- The research portfolio cap is configurable from 1 to **50 simultaneous positions** (`BACKTEST_MAX_POSITIONS`, default 50). Per-entry modeled risk defaults to 0.5% of realized balance; aggregate planned stop-risk is capped at 25%; gross notional is capped at 25× current modeled equity by default (configurable). These filters mean 50 is a ceiling, not a promise that 50 positions can be opened.
- The separate paper-trading simulator remains limited to one active position (`MAX_CONCURRENT_POSITIONS=1`). The research portfolio cap does not change paper or live execution settings.
- Reports show requested UTC window, symbols tested/eligible, 1H bars, strategy evaluations, candidates, entry attempts, maximum concurrent positions, open positions, TP hits, SL hits, time-stop exits, data-gap exits, fees, funding where available, slippage, drawdown, cost-stress runs and warnings.

## Accuracy and limitations

No historical backtest can be guaranteed 100% accurate. This simulator uses completed OHLC candles and modeled entry/exit fills, fees, slippage and available funding history. It does not replay historical order-book queue position or impact, all partial-fill behavior, the user's external positions/open orders, or exchange-exact cross-margin maintenance-margin and liquidation mechanics. The active simulator reports a conservative 1H trade-price OHLC maintenance stress proxy; this is not an exchange-exact liquidation replay. Positions still open at the end of a test are separately marked to market and included in ending equity, but not in realized P&L or closed-trade win-rate statistics. Current top-100 membership also introduces survivorship/selection bias for historical periods. Treat results as research estimates, not confirmed exchange execution.

The backtest is chronological. Entries at the same timestamp are allocated in symbol order for deterministic results. Data fetch failures, insufficient history, unverified contract metadata and missing funding are surfaced as warnings/rejections; missing data is not filled with fabricated candles or prices.

## Fundamental analysis status

`ANALYZE` provides technical/structure analysis only. Tokenomics and on-chain fundamentals are **not implemented** in this build: no market-cap, supply, FDV, unlock, on-chain activity, protocol revenue, developer activity or governance/news provider is configured. The analysis output explicitly returns `fundamentals.status=NOT_IMPLEMENTED`. Funding rates are derivatives carry data, not token fundamentals. See `docs/FUNDAMENTALS_DATA_AUDIT.md`.

## WhatsApp signals and reports

The worker is configured to evaluate the top-universe swing scanner once per completed 1H candle. A signal is sent only when S1-RFPC produces a candidate and the delivery checks pass: valid contract/price tick, sensible rounded stop/target geometry, minimum net reward/risk after modeled fees/slippage, usable top-of-book depth/spread, and an available non-adverse current funding snapshot. The system does **not** broadcast rejected or incomplete candidates as trade signals.

`SIGNAL_PUSH_ENABLED=true` enables the push feature, but the separate crypto WhatsApp policy gate still controls actual sending. By default `WHATSAPP_CRYPTO_MESSAGING_ALLOWED=false`, and the gate remains closed unless `WHATSAPP_POLICY_CLEARANCE_REF` is populated as well. Configure this only after you have confirmed that your exact business use is permitted and have the required written clearance. The allowed recipient list (`WHATSAPP_ALLOWED_SENDERS`) and valid Meta Cloud API credentials are also required. When the gate is closed, signals/reports remain in research storage and the worker logs that WhatsApp delivery was blocked; it does not claim messages were sent.

The user-facing `BACKTEST` acknowledgement is sent immediately after durable queueing. The final report is sent by the worker only when the policy gate is open; otherwise retrieve it using the protected dashboard or the stored job report route after the gate is addressed.

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
Render background worker
  - top-100 turnover universe
  - S1-RFPC 1D/12H/4H/1H scanner
  - per-symbol progress and scan events
  - durable backtest job consumer
  - WhatsApp crypto outbox dispatcher (policy-gated)
        |
        v
Shared PostgreSQL database
  - inbound/outbound message idempotency
  - scan results and active signals
  - backtest jobs, progress and reports
  - worker heartbeat and operational state
        ^
        |
FastAPI web service
  - signed WhatsApp webhook
  - command acknowledgement and job creation
  - read-only dashboard and /healthz diagnostics
```

The Render Blueprint creates one web service, one background worker and a shared PostgreSQL database. The worker polls the durable backtest queue independently using `BACKTEST_JOB_POLL_SECONDS` (default 5 seconds), separate from the 60-second market-scan cycle. Both services must use the same `DATABASE_URL`; otherwise a job queued by the web service will not be visible to the worker. The worker's persistent disk is not shared with the web service, so PostgreSQL—not the local archive disk—is the cross-service source of truth.

## Deploy and verify

1. Deploy the same repository commit to the web service and the worker.
2. Confirm both services connect to the same PostgreSQL database.
3. Confirm worker logs identify the build, database backend, `S1-RFPC-1D-12H-4H-1H`, `backtest_max_positions=50`, `paper_max_positions=1`, scanner state and WhatsApp policy gate state.
4. Check `/healthz` for worker heartbeat, latest scan summary/error, backtest latest job and policy gate.
5. Run `BACKTEST 1D`; verify `BACKTEST JOB START`, progress lines from `1/100` onward, `BACKTEST WINDOW COMPLETE` and `BACKTEST JOB COMPLETE` in worker logs. Use `BACKTEST STATUS` and `BACKTEST REPORT <JOB_ID>` to inspect queue state and final output.
6. Keep `AUTO_TRADING_ENABLED=false`, `LIVE_EXECUTION_ENABLED=false`, `LIVE_EXECUTION_BUILD_RELEASED=false`, `PAPER_TRADING_ENABLED=false` and `MAX_CONCURRENT_POSITIONS=1`. Do not open the WhatsApp crypto-message policy gate without confirming the applicable policy/clearance first.

## Local development

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\Activate.ps1
pip install -e '.[test]'
cp .env.example .env                      # Windows: copy .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For a single local process, blank `DATABASE_URL` uses SQLite. To run a local worker separately, both processes must point to the same PostgreSQL database; separate SQLite files are not a shared job queue. Set `WEB_API_TOKEN` before using dashboard research commands. Never commit `.env`, API tokens, Meta secrets or private account data.

## Validation

Run tests with `pytest -q`. Automated tests validate deterministic fixtures, parser behavior, job idempotency, data handling and simulator accounting. They do not prove current MEXC API availability, Render deployment health, WhatsApp account eligibility or future strategy profitability.


## Release notes

See [`docs/RELEASE_NOTES_v0.8.4.md`](docs/RELEASE_NOTES_v0.8.4.md) for the latest audit corrections and [`docs/RELEASE_NOTES_v0.8.3.md`](docs/RELEASE_NOTES_v0.8.3.md) for the portfolio-risk changes.
