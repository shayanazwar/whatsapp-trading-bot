# MEXC Futures RSI/EMA Bot — v0.5.0 (Backtest-first)

This release updates the Pak Trading Academy project for a **3-minute, symmetric LONG/SHORT RSI/EMA research strategy** with a cross-margin-aware historical simulator. It uses MEXC USDT-margined perpetual market data only. **Live order execution is not implemented/released and remains disabled.** The scanner records signal candidates; it does not send real orders.

## Strategy defaults

- Build completed **3-minute** bars from three contiguous, completed MEXC 1-minute bars. Do not use a forming bar.
- LONG: RSI(14) at/above 70 and rising, EMA(21) crosses above EMA(50), bullish EMA alignment, close breaks the prior high, and relative volume confirms.
- SHORT: RSI(14) at/below 30 and falling, EMA(21) crosses below EMA(50), bearish EMA alignment, close breaks the prior low, and relative volume confirms.
- EMA periods: 21/50/100; volume confirmation: 1.2× the preceding 20 completed bars.
- One simulated global position at a time; signals are evaluated on completed 3M candles and entries use the following candle.
- Initial margin assumption: $1; leverage capped at 50× and reduced to each contract's declared limit. Net target: $0.30 after estimated costs. Requested stop: $3, but liquidation can occur first.
- Fee assumption: 0.08% taker fee per side; default estimated adverse slippage: 5 bps each side. These are assumptions for simulation, not an account-specific fee quote.

This is a testable strategy hypothesis, not a profitability claim. Extreme RSI, a recent EMA cross, price breakout/breakdown and volume confirmation can be restrictive and may produce few or no signals.

## Run locally

Requires Python 3.11+.

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -e .
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/dashboard`. Set a strong local `WEB_API_TOKEN` in `.env` before using dashboard commands. Do not commit `.env` or share real secrets in chat.

Useful dashboard commands:

```text
BACKTEST 1D             # Top-100 universe, 1-day test
BACKTEST 7D             # Top-100 universe, 7-day test
BACKTEST BTCUSDT 30D    # Longer test for one specified symbol
CHART BTCUSDT 5M         # Also 15M, 1H, 4H, 8H, 12H, 1D, 3D, 1W, 1MO
STATUS
SIGNALS
```

Charts overlay EMA 21/50/100/200 and include an RSI panel with divergence markers where the pivot data is confirmed. The 3D chart is derived from three complete daily bars; 12H is derived from three complete 4H bars. Calendar month bars are fetched from MEXC's monthly interval.

## Backtest evidence

Run `BACKTEST` from the dashboard once the deployed service can reach the MEXC API. The application downloads completed historical MEXC trade candles, then fetches Fair Price and funding history only for symbols that actually receive simulated entries. Data downloads are concurrency-bounded to reduce peak memory. It writes JSON, CSV trade ledger, text summary, and an equity-curve image. If data cannot be retrieved, it must show a data-unavailable result rather than substitute synthetic prices. The tests use deterministic synthetic fixtures only to verify calculation logic; those test fixtures are **not** performance evidence.

Automated tests validate the calculation paths with deterministic fixtures. They do not prove profitable performance or substitute for a live MEXC historical-data run. No verified market-performance claim is made by this source archive. See `docs/TEST_REPORT.md`.

## WhatsApp policy gate

Meta's published WhatsApp Business policy prohibits using WhatsApp Business Services to facilitate transactions in real, virtual or fake currency. This release keeps crypto trading notifications, market-chart delivery and crypto balance messages blocked through WhatsApp Cloud API. Only safe `HELP` and `STATUS` support messages are enabled by the command allowlist. Use the protected dashboard for charts and backtest reports. Do not set a policy flag to bypass this restriction.

## Deployment (Render)

`render.yaml` describes three resources in Singapore: a web service, a separate scanner worker, and PostgreSQL. The web service binds to Render's `$PORT`; the worker holds a PostgreSQL advisory lock so a duplicate worker fails closed. PostgreSQL, not the ephemeral service filesystem, is the shared source of state.

1. Push this project to a private GitHub repository.
2. In Render, create a Blueprint from that repository and review resource costs/plans before confirming.
3. Confirm the web service health path is `/healthz`, with only one worker process for the web app. The Blueprint enables `MEXC_UNIVERSE_SCAN_ENABLED=true` and `MEXC_UNIVERSE_TOP_N=100` on the shared environment; the dedicated worker performs the scheduled scan while the web in-process scheduler remains off.
4. Keep `AUTO_TRADING_ENABLED=false`, `LIVE_EXECUTION_ENABLED=false`, `LIVE_EXECUTION_BUILD_RELEASED=false`, and `WHATSAPP_CRYPTO_MESSAGING_ALLOWED=false`. Leave `PAPER_TRADING_ENABLED=false` until reviewing results; it is simulated only and does not send orders.
5. Add a random strong `WEB_API_TOKEN`. Never expose MEXC keys to the browser or WhatsApp.
6. Wait for the web service and worker to show healthy/running. Check the logs for ticker freshness and scan summaries.
7. Confirm `/healthz` reports `scanner_enabled: true`, `scanner_worker_status: running`, and a recent `scanner_last_success`/`scanner_last_summary`. Then open `/dashboard`, enter the `WEB_API_TOKEN`, run `BACKTEST 7D`, and inspect data coverage and the CSV trade ledger. Review data coverage before trusting any result.

Git commands:

```bash
git init
git add .
git commit -m "Build MEXC RSI EMA backtest-first bot"
git branch -M main
git remote add origin <YOUR_PRIVATE_GITHUB_REPOSITORY_URL>
git push -u origin main
```

Do not include `.env`, API credentials or exported private account data. For Render's Blueprint, the generated `DATABASE_URL` and service environment values are defined in `render.yaml`.

## Current limits

- The top-universe scanner emits LONG and SHORT candidate events and persists per-symbol scan outcomes, including data errors. Optional simulated execution can be enabled with `PAPER_TRADING_ENABLED=true`; it estimates market fills from public prices and completed 3-minute candles and never submits orders.
- Historical order-book replay, exact intrabar path, account-specific liquidation tiers, and actual private-account execution have not been verified.
- The $3 stop-loss is not a maximum-loss guarantee with $1 margin and up to 50× leverage.
- A complete authenticated private-order client, position reconciliation, exchange-side protective order lifecycle, and full live execution readiness audit are required before real trading can be considered.
- Regional eligibility, your MEXC account permissions and applicable fee/contract settings must be verified directly in your account.

See `docs/TECHNICAL_RESEARCH_2026-10-29.md`, `docs/TEST_REPORT.md`, `docs/DEPLOYMENT_V0.4.md` and `docs/LIVE_TRADING_READINESS.md`.
