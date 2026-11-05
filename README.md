# RSLR Trading Bot

Python MEXC USDT-M Futures signal bot with a 1D → 12H → 4H → 1H analysis stack, independent Sweep/Reclaim and Donchian entry modules, historical backtesting, paper-signal monitoring, WhatsApp commands and PNG chart generation.

**Default operation is signal + paper mode. Live order submission is hard-locked in source code.** The environment variables `AUTO_TRADING_ENABLED` and `LIVE_EXECUTION_ENABLED` are diagnostic settings only; they cannot bypass the hard lock.

## Implemented

- Public MEXC Futures REST data client with request timeouts, bounded retries, request pacing and error reporting.
- Contract universe selection using live 24-hour turnover, contract status and MEXC contract specifications.
- Contract sizing based on contract size, tick size, quantity step, minimum volume and fee rate.
- Completed 1H candles are the canonical input. 4H bars require four contiguous completed 1H candles. 12H bars require exactly three contiguous completed 4H bars. 1D bars require 24 contiguous completed 1H candles. UTC alignment and OHLC validity are checked.
- 1D BTC regime, 12H relative-strength ranking versus BTC and cross-sectional top/bottom 35% selection, 4H trend/structure context, funding context and the two independently switchable 1H entry modules.
- Sweep/Reclaim is based on price OHLCV only. It does not claim to detect actual order-book sweeps, liquidations or hidden liquidity.
- Donchian breakout channel excludes the current signal candle from its channel levels.
- Risk budget is 0.5% of current realized equity. Initial reference equity is $100, so the initial planned loss budget is about $0.50. Quantity is reduced to respect contract minimums, exchange steps and a margin cap. Invalid or uneconomical sizing is rejected.
- Single TP at 1R **gross price distance**. Fees, slippage and funding are separately estimated and reported; net R:R is expected to be below 1:1 after costs.
- 1D, 7D and 30D historical tests use real MEXC candles and historical funding records. Backtests are chronological, use next-hour-open entries, enforce stop-first same-candle handling and gap-through-stop fills, cap simultaneous positions, and report unresolved positions separately. Generated signal counts are reported separately from trades accepted by the portfolio simulator.
- 30D walk-forward report splits the same chronological run into 70% in-sample and 30% out-of-sample segments. No parameters are tuned on the out-of-sample segment.
- SQLite for local development and PostgreSQL through `DATABASE_URL` in production. Alembic migration and persistent tables for contracts, candles, funding, universe snapshots, current OI snapshots, signals, backtest runs/trades, paper positions/outcomes, notifications and logs. The scheduler uses a database lock that is released after the job and expires automatically after a process crash.
- Paper positions are reconciled against subsequent completed 1H candles by the background worker. TP/SL collisions resolve conservatively as SL first.
- WhatsApp Cloud API webhook, sender allowlist, HMAC signature validation, command router, notification outbox, bounded retry tracking, message-ID correlation and `sent` / `delivered` / `read` / `failed` receipt updates for proactive template alerts.
- Charts use real MEXC candles and include candlesticks, volume, RSI 14, EMA 21, EMA 50, latest candle timestamp, and the most recent saved entry/SL/TP levels when available.
- Docker, local PostgreSQL compose file, Render Blueprint and GitHub CI workflow.

## Strategy controls

| Setting | Default |
|---|---:|
| Sweep/Reclaim | Enabled |
| Donchian | Enabled |
| Starting reference equity | $100 |
| Risk per trade | 0.5% of realized equity |
| Initial risk budget | About $0.50 |
| Take profit | One TP at 1R gross |
| Timeframes | 1D, 12H, 4H, 1H only |
| Maximum simultaneous positions | 3 |
| Margin cap per position | 25% of equity |
| Default leverage assumption | 2x (maximum configured 5x) |
| Max signal count | 3 per scan, 8 per rolling 24h |
| Same-symbol/side cooldown | 4 hours |
| Live order execution | Hard-locked OFF |

`TAKER_FEE_RATE` defaults to a conservative configurable assumption of 0.08% per side. The risk model uses the larger of this configured assumption and the contract's public MEXC `takerFeeRate`. Actual account-specific fee tier and execution slippage can differ; set the fee assumption to the rate applicable to the account before trusting net backtest results.

## Important limitations and interpretation

1. **No profitability claim.** The strategy is an engineering baseline. Results must be generated from MEXC data and reviewed out-of-sample before any capital is exposed.
2. **Historical universe bias.** The universe is selected using current contract availability and current 24-hour turnover. Historical delistings and point-in-time contract membership are not reconstructed, so backtests have survivorship/universe bias.
3. **Data sample limit.** MEXC documents a maximum 2,000 kline records per request. A 30D test requests 42 days of warm-up plus 30 days of evaluation (about 1,728 hourly bars, before any data gaps). Missing or incomplete bars reduce usable data; the program fails or skips symbols rather than fabricating candles.
4. **OHLCV execution approximation.** The simulator is 1H bar-based, not tick-level replay. A same-candle TP/SL collision is counted as SL. Gap-through-stop is filled at the worse bar open. It cannot reconstruct the actual intrabar path, queue position or order-book liquidity.
5. **Funding.** Settled historical funding records are used during backtest trades where records overlap the holding interval. Sizing uses a conservative current-rate estimate; actual future funding can change.
6. **Open interest.** Current `holdVol` snapshots are stored when returned by MEXC. Historical OI is not fabricated and is not a required strategy gate in this release.
7. **Paper mode.** Signal observations use the signal candle's close as a reference entry and are followed against subsequent completed 1H bars. Paper results are not exchange fills.
8. **Live execution.** `MEXCExecutionAdapter.prepare()` can prepare an order description, but `submit_order()` always raises `ExecutionLocked`. There is no private order-transport path in this release. Do not interpret the scaffold as a live trading system.
9. **WhatsApp proactive alerts.** Command replies work during an eligible user-initiated messaging window. Proactive scheduled alerts require an approved WhatsApp message template and `WHATSAPP_SIGNAL_TEMPLATE_NAME`. Without an approved template, signals are stored but proactive sends are skipped.
10. **External credentials.** No MEXC private API key is required for public data. Meta webhook and WhatsApp credentials must be supplied by the project owner in environment variables; they are not included.

## Local setup

Python 3.11+ is required.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
uvicorn rslr.web:app --host 0.0.0.0 --port 8000
```

The local default is SQLite. To use PostgreSQL locally, start the services in `docker-compose.yml` and set `DATABASE_URL` to the PostgreSQL URL shown there. Do not commit `.env`.

## Commands

| Command | Function |
|---|---|
| `/menu`, `/help` | Show available commands |
| `/status` | Bot mode, timeframe and risk status |
| `/scan` | Fetch eligible MEXC markets and generate new signals |
| `/signals` | Latest saved signals |
| `/backtest 1D` | Real 1-day historical simulation |
| `/backtest 7D` | Real 7-day historical simulation |
| `/backtest 30D` | Real 30-day historical simulation |
| `/walkforward` | 30D chronological 70/30 validation report |
| `/chart BTC_USDT` | Latest 1H chart |
| `/chart BTC_USDT 4H` | 4H chart |
| `/chart BTC_USDT 12H` | Built 12H chart |
| `/chart BTC_USDT 1D` | 1D chart |
| `/performance` | Latest saved backtest report |
| `/settings` | Active configuration |
| `/auto` | Confirms live execution remains locked |

CLI equivalents:

```bash
rslr scan
rslr backtest 1D
rslr backtest 7D
rslr backtest 30D
rslr walk-forward 30D
```

## WhatsApp configuration

Set these variables in Render or `.env`:

- `WHATSAPP_ACCESS_TOKEN`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_VERIFY_TOKEN`
- `WHATSAPP_APP_SECRET`
- `WHATSAPP_ALLOWED_NUMBERS` (comma-separated sender numbers, digits only, including country code)
- `WHATSAPP_GRAPH_VERSION` (currently defaulted to `v26.0`; keep it aligned with the Meta app/API version in use)
- `WHATSAPP_SIGNAL_TEMPLATE_NAME` and `WHATSAPP_TEMPLATE_LANGUAGE` for proactive notifications

Webhook callback URL: `https://YOUR_HOST/webhook/whatsapp`. Verify using the configured verify token and subscribe to the `messages` webhook field. POST requests fail closed unless `X-Hub-Signature-256` validates against `WHATSAPP_APP_SECRET`. The sender allowlist must be populated or all incoming commands are ignored.

## Render deployment

1. Create a blank GitHub repository named `RSLR_TRADING_BOT` under the target GitHub account.
2. Push this project to the repository (commands below).
3. Connect the repository to Render and apply `render.yaml`.
4. Add WhatsApp variables through Render's environment settings. Do not add MEXC private trading keys; this release does not place orders.
5. Confirm `/health`, webhook verification, `/help`, a chart command and a 1D backtest.

The Render Blueprint creates one web service, one background worker and a PostgreSQL database. Keep exactly one worker instance unless the scheduler lock configuration is reviewed for multi-instance behavior.

## Tests and migrations

```bash
python -m compileall -q rslr migrations
python -m pytest -q
alembic upgrade head
```

The tests use deterministic local fixtures and mocked HTTP responses. They do not certify live MEXC connectivity, Meta webhook configuration, Render deployment or strategy profitability. The build environment had no package-index access for dependency resolution, so a fully resolved transitive `requirements.lock` was not fabricated; `pyproject.toml` is the authoritative dependency specification.

## GitHub upload

Create a blank repository named `RSLR_TRADING_BOT` on GitHub, then from this folder:

```bash
git init -b main
git add .
git commit -m "Initial RSLR MEXC futures signal bot"
git remote add origin https://github.com/shayanazwar/RSLR_TRADING_BOT.git
git push -u origin main
```

Never upload `.env`, tokens, or API secrets.

## Build audit

See [`AUDIT_REPORT.md`](AUDIT_REPORT.md) for the verified test results, external setup blockers and known limitations.
