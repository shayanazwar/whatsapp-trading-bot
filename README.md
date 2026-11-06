# BTCUSDT MEXC Signal & Backtest Bot — v1.0.0

A single-market, 1-minute **signal-only** application for MEXC Futures. It monitors BTC_USDT, sends closed-candle LONG/SHORT alerts to WhatsApp, and runs fresh-data historical backtests when an authorized WhatsApp user sends a command.

**This release cannot place, edit, cancel, or close exchange orders.** It contains no MEXC private API credentials, no trading-order endpoint, and no paper-order execution loop.

## 1. What is included

- MEXC Futures `BTC_USDT` only.
- `Min1` candles only. Signals are evaluated from completed candles; the current incomplete candle is never used to generate a signal.
- Fixed EMA 6 / 12 / 21 alignment and close position.
- Live default RSI 9 with 65/35 thresholds; RSI must rise for LONG or fall for SHORT.
- Backtest comparison of RSI 9/14 × thresholds 65/35 or 70/30.
- TP comparison at 0.50%, 0.75%, and 1.00% favourable price movement.
- Maximum modelled margin budget 0.50 USDT per entry. Contract quantity is rounded down to MEXC's current volume step.
- 200x is a sizing/backtest assumption only. It is not a promise that the account, region, symbol, or notional tier permits 200x.
- No normal stop-loss, trailing stop, partial take-profit, or time exit.
- No volume gate.
- Plain-text WhatsApp output; no dashboard or report export.
- Persistent SQLite state for webhook idempotency and signal deduplication.
- Render Blueprint, Dockerfile, Procfile, and GitHub Actions test workflow.

## 2. Strategy rules

**LONG** requires all of the following on the latest completed 1-minute candle:

1. EMA 6 > EMA 12 > EMA 21.
2. Candle close is above all three EMAs.
3. RSI is at or above the configured upper threshold.
4. RSI is greater than its value on the immediately previous completed candle.

**SHORT** is the inverse: EMA 6 < EMA 12 < EMA 21, close below all three, RSI at/below the lower threshold, and RSI lower than the preceding candle's RSI.

EMA uses an SMA seed followed by the standard alpha `2/(period+1)` recursion. RSI uses Wilder smoothing, initialized with the arithmetic mean of the first N gains/losses. Indicators use candle close prices. A signal fires on a false-to-true directional setup edge; persistent true conditions do not repeatedly alert.

### Live signal safety rules

- Abnormal movement pauses **new** signals at a 5-minute absolute close-to-close move of 1.00% or a 1-minute candle range of 0.60%.
- Resume requires the 5-minute move to remain below 0.50% and the 1-minute range below 0.60% for 15 continuous minutes.
- On startup/restart, the monitor synchronizes with the latest completed candle and suppresses the current setup. It does not send missed/stale alerts.
- An alert is suppressed when current MEXC last price is more than 0.20% away from the signal candle close, or the ticker timestamp is stale (older than 120 seconds) / in the future.
- Alerts are sent only to `WA_RECIPIENTS`. At-most-once behavior is used for outgoing signal IDs; an ambiguous send failure is not blindly retried because that could duplicate an alert.
- Live notifications are **alerts only**. The application cannot know whether a user manually opened/closed a real MEXC position, so it does not lock live alerts behind a pretend paper position. Backtests use one hypothetical position at a time.

### WhatsApp live message

```text
BTCUSDT SIGNAL

Price: [completed candle close]
EMAs: Broken UP / Broken DOWN
RSI: [value]
SL: NONE
TP: [indicative target from signal close]
Direction: LONG / SHORT
Timeframe: 1M
Candle: [UTC timestamp]
ALERT ONLY — NO ORDER PLACED
```

The live TP is indicative and calculated from the signal candle close. The actual next-candle entry is not yet known when the signal is generated.

## 3. Backtesting — read the limitations

Supported WhatsApp commands:

```text
/backtest 1d both
/backtest 1d long
/backtest 1d short
/backtest 7d both
/backtest 7d long
/backtest 7d short
/backtest 30d both
/backtest 30d long
/backtest 30d short
```

Each command downloads fresh MEXC Futures Min1 candles. Local cached candles are not used as the source for a new run. A run compares 12 variants: four RSI configurations × three TP percentage targets.

Backtest conventions:

- A candidate signal is evaluated on a completed candle.
- Entry approximation is the next 1-minute candle open plus one configured tick for LONG, or minus one tick for SHORT.
- If the approximated entry is more than 0.20% away from the signal close, the setup is rejected as stale.
- TP is calculated from the approximated entry. The first later candle whose high/low touches the TP threshold is recorded as `TP touched`.
- Exit estimate applies one adverse tick and the configured taker fee to each side.
- A single hypothetical position is allowed at a time. With no normal SL, a position that never touches TP remains unresolved through the end of the sample and blocks later entries.
- The same abnormal-movement pause/recovery rules and directional setup-edge deduplication are applied to historical entries.

**Backtest model limitations — critical:**

1. Public 1-minute OHLC cannot reconstruct the exact first executable ask/bid or historical order-book slippage. Next-open ± tick is a disclosed approximation, not exact execution.
2. Liquidation is **NOT MODELLED**. MEXC's public contract metadata contains current risk information, but this build does not reconstruct account-specific historical risk tiers and fair-price liquidation events. The historical TP touch may have occurred after a real position would have been liquidated.
3. Historical funding cashflows are **NOT MODELLED**.
4. Same-candle order of events and real fill priority cannot be established from 1-minute OHLC alone.
5. Therefore TP-touch count is not a confirmed win count. Win rate, Profit Factor, total strategy PnL, drawdown, liquidation streak, and loss streak are withheld. The reported net PnL is only for the subset that touched TP under the stated fee/slippage assumptions; it excludes unresolved outcomes and is not strategy profitability.
6. The 0.08% per-side taker fee default is a configurable modelling assumption. Current public contract fee metadata is displayed for comparison; the user's actual fee tier and manual-app fee may differ.
7. The 100 USDT planning balance is a reference only. The v1.0 backtest does not compound balance or model total-account drawdown.
8. A 1D / 7D / 30D period is not evidence of a durable edge. Further out-of-sample and longer-period validation is required.

The system checks every expected candle timestamp. Missing, duplicate, invalid, or out-of-order candles and failed API segments make the data incomplete; the backtest then stops and reports the gap rather than inventing candles.

## 4. Architecture

```text
MEXC Futures public REST API
        │
        ├── Min1 closed candles ──> candle validation ──> EMA/RSI strategy
        │                                              │
        │                                              ├── abnormal-move gate
        │                                              ├── stale-price check
        │                                              └── edge dedupe ──> WhatsApp alert
        │
        ├── contract metadata ──> quantity sizing / leverage guard
        │
        └── fresh historical candles ──> backtest engine ──> plain-text variants

WhatsApp Cloud API webhook ──> signature verification ──> sender allowlist
                                                │
                                                └── command parser ──> backtest job / help / status

SQLite WAL ──> processed-message IDs, sent-signal IDs, monitor and pause state
```

The service is intentionally single-instance. SQLite plus a Render persistent disk is used for durable state. This is not a multi-instance architecture.

## 5. Setup locally

Requires Python 3.12+.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Edit `.env`. Never commit it. For a local server without WhatsApp or the market monitor:

```bash
DATABASE_PATH=./data/btc_mexc_bot.sqlite3 MONITOR_ENABLED=false uvicorn app.main:app --host 127.0.0.1 --port 10000
```

Health endpoint: `http://127.0.0.1:10000/healthz`.

## 6. WhatsApp configuration

The integration uses the WhatsApp Cloud API send-message endpoint and webhook HMAC signature verification. No WhatsApp policy websites were consulted for this implementation.

Set these variables in Render's environment settings, not in GitHub:

- `WA_PHONE_NUMBER_ID`
- `WA_ACCESS_TOKEN`
- `WA_VERIFY_TOKEN` — long random verification token you create.
- `WA_APP_SECRET` — Meta app secret, used only to verify webhook signatures.
- `WA_RECIPIENTS` — comma-separated recipient phone numbers in digits-only international format.
- `AUTHORIZED_WA_IDS` — comma-separated sender IDs permitted to run commands. Other senders are silently ignored.
- `GRAPH_API_VERSION` — defaults to `v26.0`; verify that the chosen Graph API version is enabled/available in your Meta app before deployment.

Webhook callback URL: `https://YOUR-RENDER-SERVICE.onrender.com/webhook`.

Subscribe the Meta app webhook to incoming WhatsApp `messages`. Configure the same verification token in Meta and Render. POST events must include a valid `X-Hub-Signature-256`; requests without a matching HMAC are rejected. Duplicate inbound message IDs are ignored.

## 7. Render deployment

The included `render.yaml` creates a Docker web service, persistent disk mounted at `/var/data`, and a paid `starter` plan. This is intentional: Render free web services spin down after 15 minutes without inbound traffic, have ephemeral local storage, and cannot attach persistent disks. Confirm current charges in your Render dashboard; no fixed monthly cost is quoted here.

1. Extract the ZIP and create a **private** GitHub repository.
2. Upload/commit the contents of this project folder to the repository root. Do not upload `.env`, tokens, or app secrets.
3. In Render, create a new Blueprint from the repository and apply `render.yaml`.
4. Add the WhatsApp environment variables listed above.
5. Deploy and verify `/healthz` reports `status: ok`, `whatsapp_configured: true`, and `signal_recipients_configured: true`.
6. Configure and verify the Meta webhook URL and verification token.
7. Send `/help` and `/status` from an authorized WhatsApp sender.
8. Before relying on signal notifications or research results, manually verify MEXC data responses, current fee settings, contract sizing, and a short backtest.

GitHub push and Render deployment are not performed by this ZIP build. They require your own GitHub/Render/Meta account access.

## 8. Environment variables

All supported settings and safe defaults are in `.env.example`. Key settings:

| Setting | Default | Purpose |
|---|---:|---|
| `MAX_MARGIN_USDT` | `0.50` | Maximum isolated-margin sizing budget, never exceeded |
| `MODEL_LEVERAGE` | `200` | Hypothetical sizing leverage; account eligibility not verified |
| `LIVE_TP_MOVE_PCT` | `0.50` | Indicative live TP price movement from signal close |
| `BACKTEST_TP_PCTS` | `0.50,0.75,1.00` | Historical TP comparison matrix |
| `BACKTEST_TAKER_FEE_RATE` | `0.0008` | Assumed taker fee per side |
| `BACKTEST_SLIPPAGE_TICKS` | `1` | Adverse tick adjustment per side |
| `SIGNAL_EXPIRY_DEVIATION_PCT` | `0.20` | Reject setup if entry/current price moved too far |
| `ABNORMAL_5M_MOVE_PCT` | `1.00` | Pause threshold |
| `ABNORMAL_1M_RANGE_PCT` | `0.60` | Pause threshold |
| `RECOVERY_5M_MOVE_PCT` | `0.50` | Recovery threshold |
| `RECOVERY_MINUTES` | `15` | Continuous recovery duration |

`MAX_MARGIN_USDT` is a maximum budget. Contract quantity is rounded down to MEXC's `volUnit`, and actual margin may be below 0.50 USDT. No order is sent.

## 9. Testing

```bash
pip install -r requirements-dev.txt
pytest -q
python -m compileall -q app tests
```

Automated tests use deterministic fixtures and mocked HTTP responses. They do not establish live MEXC uptime, account eligibility, actual fills, webhook delivery, or live Render operation.

## 10. Release boundary

**Build status:** deployable source package, version `1.0.0`.

**Not production-certified:** live MEXC integration, live Meta message delivery, account-specific leverage/fees, liquidation/funding modelling, long-horizon/out-of-sample profitability, and actual Render deployment remain unverified. Do not use this release to place trades; it has no order-placement capability.
