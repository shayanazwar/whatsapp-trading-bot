# Pak Trading Academy Dual-Channel Trading Bot

A dual-channel cryptocurrency assistant with three separate paths:

1. Manual commands available on WhatsApp and Telegram backed by MEXC Futures (`PRICE`, `ANALYZE`, `CHART`, `ALERT`, `SEARCH`).
2. A deterministic MEXC Futures market scanner that uses closed candles and sends trade signals when the configured confluence/risk gates pass.
3. A separately isolated MEXC Futures execution adapter. **Live execution is disabled in this build.**

## Current architecture

```text
WhatsApp Cloud API + Telegram Bot API
        |
        v
   app.main webhooks
        |
        v
      app.bot
   /     |      \
PRICE  ANALYZE  CHART/ALERT
  |        |
MEXC Futures  MEXC Futures

MEXC automation (independent)

MEXC Futures market data
        |
        v
   Data integrity / closed candles
        |
        v
   Universe + BTC filter
        |
        v
   1D -> 4H -> 1H -> 15M
        |
        v
   Deterministic analysis + structural targets
        |
        v
   Live MEXC execution-quality context
        |
        v
   82/100 + RR>=2 + Families>=5/6
        |
        v
   Signal validator
        |
        +----> SQLite signal state + WhatsApp + Telegram
        |
        +----> execution gate (disabled)
```

## Safety state shipped by default

```text
SCANNER_ENABLED=false
AUTO_SIGNAL_ENABLED=false
AUTO_TRADE_ENABLED=false
ALLOW_LIVE_EXECUTION=false
```

Do not change the live-trading switches until the scanner has been observed and the execution path has been separately validated.

## MEXC API notes

The project uses the current MEXC Futures API base:

```text
https://api.mexc.com
```

Current documented public endpoints used by the scanner include:

```text
GET /api/v1/contract/ping
GET /api/v1/contract/detail/country
GET /api/v1/contract/ticker
GET /api/v1/contract/kline/{symbol}
```

Current documented private endpoints prepared in the API client include:

```text
GET  /api/v1/private/account/assets
GET  /api/v1/private/position/open_positions
GET  /api/v1/private/position/position_mode
POST /api/v1/private/position/change_leverage
POST /api/v1/private/order/create
GET  /api/v1/private/order/get/{orderId}
POST /api/v1/private/stoporder/place
```

Private requests follow the current documented OPEN-API signing process: sorted GET/DELETE query parameters or the exact POST JSON string are combined with Access Key + timestamp and HMAC-SHA256 signed.

MEXC does not currently provide a sandbox/test environment, so this build deliberately does not send real orders.

## Scanner logic

The scanner uses a deterministic hierarchy rather than a large stack of interchangeable indicators:

```text
1D context
  -> 4H regime
  -> 1H direction / protected structure
  -> 15M BOS + post-BOS retest
  -> 5M trigger
  -> momentum / RVOL / volatility
  -> structural target path
  -> SL / TP / RR
  -> hard rejection gates
  -> MEXC spread / reference-price / funding / depth / trade-flow context
  -> grouped 100-point score
  -> final validation
```

The default hard gates are `Score >= 82/100`, `RR >= 2.0`, and at least `5/6` confirmation families. Only fully closed candles are used for decisions. The engine does not fabricate fixed targets and does not claim a guaranteed win rate.

The current scanner is designed to reject most apparent setups rather than force a signal every cycle. A clean `valid=0` result is not, by itself, a reason to weaken the gates; the logs now expose technical rejection stages for diagnosis.

## Dual-channel messaging

WhatsApp and Telegram can run at the same time.

Configure Telegram:

```text
TELEGRAM_BOT_TOKEN=<your token>
TELEGRAM_ALLOWED_USERS=<chat id>,<chat id>
AUTO_SIGNAL_TELEGRAM_RECIPIENTS=<chat id>,<chat id>
```

`AUTO_SIGNAL_RECIPIENTS` remains the WhatsApp recipient list. Telegram automatic-signal recipients are stored internally with the `tg:` prefix.

For Render, either set `TELEGRAM_WEBHOOK_URL` to:

```text
https://<your-render-service>.onrender.com/telegram/webhook
```

or leave it blank and set the Telegram webhook manually with Telegram's `setWebhook` method.

## Automatic signals

Set:

```text
SCANNER_ENABLED=true
AUTO_SIGNAL_ENABLED=true
AUTO_TRADE_ENABLED=false
```

Then configure the recipients:

```text
AUTO_SIGNAL_RECIPIENTS=923xxxxxxxxx,923yyyyyyyyy
```

When WhatsApp recipients are blank, `ALLOWED_USERS` is used. When Telegram recipients are blank, `TELEGRAM_ALLOWED_USERS` is used.

Example signal shape:

```text
🚨 TRADE SIGNAL

━━━━━━━━━━━━━━━━
SOL_USDT — LONG
━━━━━━━━━━━━━━━━

📍 Entry
$84.20

🛑 Stop Loss
$82.90

🎯 TP1
$86.80

🎯 TP2
$89.40

📊 Risk/Reward
1:2.00

📈 4H
BULLISH

📊 1H
HH/HL

⚡ 15M
BULLISH BOS

EMA 21/50
BULLISH

RSI
58.0

Volume
INCREASING

Score
100/100

Families
6/6
━━━━━━━━━━━━━━━━
```

Exact values come from the deterministic analysis; no fake values are inserted.

## Manual commands

```text
HELP
PRICE BTCUSDT
ANALYZE BTCUSDT
CHART BTCUSDT 1H
ALERT BTCUSDT ABOVE 120000
ALERTS
DELETE 12
DELETE ALL
SEARCH PEPE
```

All market/trading functionality is MEXC Futures based. WhatsApp and Telegram are interface/notification layers.

## Local checks

```bash
python -m compileall -q .
python -m pytest -q
```

The tests include package imports, alert/message dedupe, chart rendering, MEXC signature construction, candle look-ahead protection, confluence/level validation, position sizing, and scanner fault isolation.

## Render deployment

For the current single-service design, run one web service so only one scanner loop is active. The scheduler is idempotent within the process and starts only when `SCANNER_ENABLED=true`.

Environment variables should be entered in Render's encrypted environment-variable settings. Never commit `.env`, API keys, or secrets to GitHub.

The current Dockerfile starts:

```text
uvicorn app.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}
```

## MEXC key safety

Only store:

```text
MEXC_ACCESS_KEY
MEXC_SECRET_KEY
```

in Render environment variables. Never paste the Secret Key into WhatsApp, GitHub, chat, logs, or source files.

## Live trading status

`app/automation/executor.py` contains the verified API payload builder and current endpoint adapter, but `LIVE_IMPLEMENTED = False` deliberately prevents real order placement. Before enabling live execution, the remaining work is post-fill position reconciliation and verified protective SL/TP installation using the current MEXC endpoint behavior on the user's account.
