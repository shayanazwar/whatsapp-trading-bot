# PAK TRADING ACADEMY — MEXC Futures Research Platform v0.7.0

Research-only MEXC USDT perpetual scanner, strategy comparison engine, historical backtester, and protected dashboard. **Live order execution is disabled and not implemented in this release.**

## What this release contains

- Dynamically discovers up to 100 active USDT-settled perpetual contracts ranked by 24-hour turnover. `DEFAULT_SYMBOLS` is retained only for legacy paper features; it does not select the top-universe scanner.
- Captures completed MEXC 1-minute candles and builds UTC-aligned 3-minute candles only from three complete, contiguous 1-minute bars. Incomplete or conflicting candles are rejected.
- Stores raw 1M and derived 3M candles in a gzip archive on the dedicated Render Background Worker persistent disk. PostgreSQL is the shared source of truth for signals, scan outcomes, worker health and durable backtest jobs. The worker-owned disk is not shared with the web service.
- Runs two separately identified strategy variants with the same market-data and portfolio simulator:
  - **CONTROL:** existing 3M RSI/EMA baseline, retained as a benchmark. It is not asserted to be profitable.
  - **CANDIDATE:** 15M trend context with 3M pullback/reclaim, ATR-based stop/target hypothesis, volume/volatility filters, and net reward/risk validation.
- Runs portfolio-aware research simulations with one active bot position at a time, a $100 starting-wallet default, a $1 maximum initial-margin cap, 0.5% candidate risk budget, Cross Margin model, configured leverage limits, contract risk tiers where available, fees, slippage, funding and Fair Price liquidation checks where historical data is available.
- Queues `BACKTEST` requests as durable jobs in PostgreSQL. The web service acknowledges and reads job status; the background worker downloads/loads history and performs the simulation.
- Requires explicit historical data coverage. A 7D request cannot silently become 1D. A top-100 run fails rather than reporting a partial universe below the configured coverage gate.
- Generates report text plus JSON/CSV/TXT audit artifacts on the worker's persistent report directory.

The candidate strategy's thresholds are research hypotheses, not validated optimal parameters. The report includes data coverage, missing support-data warnings, current-universe survivorship-bias disclosure, and simulator limitations.

## Architecture

```text
MEXC Futures REST
      |
      v
Render Background Worker ------------------------------+
  - top-100 universe discovery                           |
  - 1M candle capture                                    |
  - validated 1M -> 3M aggregation                        |
  - 3M scan + 15M context                                 |
  - per-symbol outcomes and signal persistence           |
  - durable backtest job consumer                         |
  - worker-owned persistent candle archive                |
      |                                                   |
      +---------------------> PostgreSQL <----------------+
                               - signals
                               - scan cycles/outcomes
                               - worker heartbeat
                               - backtest jobs/results
                               - events/idempotency
                                      ^
                                      |
                         Render FastAPI Web Service
                         - WhatsApp webhook support
                         - dashboard and health endpoint
                         - enqueue/read backtest jobs
                         - no heavy backtest execution
```

## Backtest commands

```text
BACKTEST 1D                 # Top-100 CONTROL + CANDIDATE
BACKTEST 7D                 # Top-100 CONTROL + CANDIDATE
BACKTEST CONTROL 7D         # Control strategy only
BACKTEST CANDIDATE 7D       # Candidate strategy only
BACKTEST BTCUSDT 30D        # Longer single-symbol run
BACKTEST STATUS             # Latest job status/progress
BACKTEST REPORT <JOB_ID>    # Retrieve a completed job report
```

Top-universe backtests are limited to 1D/7D. Longer windows require one symbol. A job fails if actual data coverage or valid-universe coverage is below configured thresholds; it does not substitute a shorter period or fabricate candles.

Historical MEXC klines are fetched in bounded pages (up to 1,800 candles/request in this client, below the documented 2,000-point API maximum). A 2,000-candle response cap is per request, not a claim that the exchange only retains 2,000 candles of history. API history availability may still vary by symbol/endpoint; actual coverage is checked for every run.

## Scanner and monitoring

The Render Blueprint enables the scanner on the dedicated worker (`MEXC_UNIVERSE_SCAN_ENABLED=true`, `MEXC_UNIVERSE_TOP_N=100`). The web service's optional in-process scheduler remains disabled to avoid duplicate scanning.

Open `/healthz` to inspect database status, build version, scanner configuration, worker heartbeat, last scan attempt/success, summary, last error, live-execution flags, and WhatsApp policy gate. Worker logs should include `TOP-100 MTF-TPR 3M SCAN COMPLETE` with discovered/processed/success/error/signal counts.

Each symbol is reported as `SIGNAL`, `NO_SIGNAL`, `DATA_ERROR`, or `INSUFFICIENT_HISTORY`. A zero-signal cycle with complete data is different from a data-fetch failure.

## WhatsApp restrictions

The crypto-message gate is **closed by default**. Do not set `WHATSAPP_CRYPTO_MESSAGING_ALLOWED=true` to bypass Meta policy. Meta's WhatsApp Business Messaging Policy restricts use of WhatsApp Business to promote or facilitate exchange of real or virtual currency. This build therefore does not send crypto signal pushes or crypto backtest reports through the WhatsApp Business Platform. The protected web dashboard is the research interface. Only permitted support commands such as `HELP` and `STATUS` are enabled for WhatsApp.

## Local development

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\\Scripts\\Activate.ps1
pip install -e '.[test]'
cp .env.example .env                      # Windows: copy .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For local-only development, `DATABASE_URL` may be blank and SQLite is used by the single process. Production requires `DATABASE_URL` (PostgreSQL). To run the worker locally, use a separate terminal with `APP_ROLE=worker`; configure writable `MARKET_DATA_DIR` and `REPORTS_DIR`. Do not run a local worker and production worker against the same local SQLite file.

Set `WEB_API_TOKEN` in `.env` before using protected dashboard actions. Never commit `.env`, API tokens, Meta secrets or private account data.

## Render deployment notes

The Blueprint creates a PostgreSQL database, a FastAPI web service, and one background worker. The worker alone mounts `/var/data`; it stores the gzip candle archive and local audit artifacts there. The web service does not depend on that filesystem. Both services use PostgreSQL for shared state.

Review plans/pricing before applying the Blueprint. After deployment:

1. Verify the web service and worker use the same Git commit/build version.
2. Verify PostgreSQL connectivity from both services.
3. Verify the worker log says `auto_scan=true` and `APP_ROLE=worker` data paths resolve under `/var/data`.
4. Verify `/healthz` shows worker heartbeat and a recent top-100 scan summary.
5. Run `BACKTEST 1D` then `BACKTEST 7D` from the protected dashboard. Inspect requested/actual UTC window, valid symbol count, candle coverage and warnings before considering the metrics.
6. Keep `AUTO_TRADING_ENABLED=false`, `LIVE_EXECUTION_ENABLED=false`, `LIVE_EXECUTION_BUILD_RELEASED=false`, `PAPER_TRADING_ENABLED=false`, and `WHATSAPP_CRYPTO_MESSAGING_ALLOWED=false` until separately reviewed.

## Important limitations

- No live MEXC account or order was used. Live execution remains disabled.
- Historical fills are synthetic; no historical order-book queue position or guaranteed fill is available.
- Cross-margin simulation is intentionally limited to one bot position and does not replay the user's external positions, isolated positions or open orders. Contract risk-tier and liquidation-fee data may be unavailable; fallback use is disclosed in the report.
- Fair Price history may be unavailable for a symbol; when missing, liquidation checks for that symbol are unavailable and flagged, not invented.
- Funding history may be incomplete; unknown funding is flagged and cannot be treated as exact net performance.
- The current top-100 turnover universe is applied to historical windows. Point-in-time historical membership is unavailable, so survivorship/selection bias is disclosed.
- Current API fee rates in environment variables are defaults/assumptions. Contract/account-specific rates can differ. Verify your own current MEXC fee tier.
- Automated tests use deterministic fixtures and mocks. They do not establish live MEXC reachability, actual Render memory use, WhatsApp eligibility, or strategy profitability.

See `docs/TEST_REPORT.md` and `docs/IMPLEMENTATION_REPORT_V0.7.0.md`.
