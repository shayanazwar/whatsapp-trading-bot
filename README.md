# KRONOS-MEXC-INTELLIGENCE

**PAK TRADING ACADEMY — KRONOS INTELLIGENCE** is an independent MEXC USDT-M perpetual-futures quantitative research platform. It combines MEXC public market data, the official Kronos financial-candlestick foundation model, a transparent multi-timeframe setup engine, a sequence-preserving historical simulator, event-driven backtesting, an authenticated research dashboard and paper-only signals.

> **Current release status: source package prepared; external model download, live exchange verification and cloud deployment require network/account access. This is not a claim of production deployment or trading profitability.**

## Capabilities in this release

- MEXC Futures public REST adapter for contract metadata, tickers, K-lines, depth, fair price and funding observations.
- MEXC Futures WebSocket client with subscription, ping, reconnect and bounded backoff.
- UTC candle validation, incomplete-candle exclusion, gap checks and deterministic 12H aggregation from three contiguous 4H bars.
- Original 1D regime → 12H confirmation → 4H setup → 1H trigger strategy. No old trading project code is used.
- Kronos adapter pinned to an upstream source commit and Hugging Face model/tokenizer revisions. Its inference implementation retains individual generated paths rather than averaging the sample dimension.
- 10,000-path moving-block OHLC bootstrap, TP/SL barrier evaluation and fee/slippage/funding-aware calculations.
- Event-driven historical backtester that only uses higher-timeframe bars completed at each decision time and enters no earlier than the following 1H bar open.
- Durable database jobs with leases, retries and worker roles; forecast paths compressed for storage.
- Authenticated React dashboard with scanner, analysis, backtesting, signals, risk and forecast desk tabs. A canvas-based forecast visualizer draws historical candles, individual future paths and quantile bands.
- WhatsApp crypto-futures signal delivery is deliberately hard-disabled because Meta's current Business Messaging Policy prohibits promotion or facilitation of virtual-currency exchange. In-app dashboard alerts remain available.
- No live order placement, no private MEXC trading key requirement and no withdrawals.

## Run locally

Requirements: Python 3.11+, Node.js 20+, PostgreSQL for multi-worker deployment (SQLite is supported for single-process local development).

```bash
cp .env.example .env
# Change APP_SECRET_KEY and ADMIN_PASSWORD before exposing the app.
python -m venv .venv
source .venv/bin/activate             # Windows: .venv\\Scripts\\activate
pip install -e '.[dev,inference]'
python scripts/install_kronos.py       # requires network access to GitHub/Hugging Face
python scripts/download_models.py     # downloads pinned weights; requires network access
python -m app.migration_lock
npm --prefix frontend install
npm --prefix frontend run build
python scripts/build_frontend.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Default local login is `admin` / `change-this-local-password`; change it before any non-local use. API docs are at `/docs`.

Run workers in separate terminals:

```bash
python -m app.worker --role market
python -m app.worker --role compute
```

Run tests and static checks:

```bash
pytest -q
ruff check app tests scripts
npm --prefix frontend run build
```

## Example research operations

The dashboard supports symbol analysis, forecast requests, market scans and backtest jobs. Authenticated API endpoints include:

- `POST /api/analyze` — queue full symbol analysis.
- `POST /api/forecast` — queue a Kronos forecast.
- `POST /api/backtests` — queue a historical backtest.
- `POST /api/scan` — queue a MEXC universe scan.
- `GET /api/jobs` — inspect jobs and progress.
- `GET /api/signals` — inspect paper-only signals.
- `GET /api/forecasts/{id}?include_paths=true` — inspect stored individual forecast paths.
- `GET /api/backtests/{id}` — inspect a full report.
- `GET /api/backtests/{id}/trades.csv` — download trades.

## Forecast and probability terminology

Kronos path shares are **model sample frequencies**, not calibrated real-world event probabilities. The system keeps raw Kronos forecast paths and historical bootstrap paths separate. It must not blend both counts into a single probability. The calibration state starts as `UNVALIDATED`; only untouched walk-forward observations can justify calibrated event-probability labels.

## Exchange details

The current MEXC Futures API base used here is `https://api.mexc.com`. Use the documented Futures contract endpoints, not Spot endpoints. MEXC does not publish a native 12H K-line interval in its current interval list; the app aggregates 12H from three contiguous completed 4H candles aligned to UTC. Historical K-lines are fetched in bounded windows because the API limits each response to 2,000 points.

## Deployment

See `docs/RENDER_DEPLOYMENT.md`. The Render Blueprint provisions a web service, market worker, CPU inference/compute worker and PostgreSQL database. Render's published service plans are CPU/RAM plans, not a GPU inference tier. The project therefore uses CPU inference by default and exposes an optional external GPU inference integration boundary. Benchmark model loading and sample generation before choosing a production instance size.

## WhatsApp policy

See `docs/WHATSAPP_COMPLIANCE.md`. WhatsApp signal sending is intentionally blocked in code, not merely switched off by an environment variable. Do not reword crypto-futures messages, use an unofficial client or route through another identity to evade Meta's policy. This project uses the authenticated web dashboard for its own signal feed.

See [`TEST_REPORT.md`](TEST_REPORT.md) for exact checks performed, checks blocked by this environment, and known functional limitations.

## Security and operating mode

- `PAPER_MODE=true`
- `LIVE_TRADING_ENABLED=false`
- `AUTOMATIC_ORDER_EXECUTION=false`
- No MEXC private trade credentials are implemented.
- Never put `.env`, tokens, passwords, model weights or database files into Git.

Read `docs/ARCHITECTURE.md`, `docs/TRADING_STRATEGY.md`, `docs/KRONOS_INFERENCE.md`, `docs/BACKTESTING_METHODOLOGY.md`, `docs/FORECAST_VALIDATION.md`, `docs/RISK_MANAGEMENT.md` and `docs/OPERATIONS.md` before interpreting research output.
