# Pak Trading Academy WhatsApp Trading Bot

A WhatsApp-first cryptocurrency assistant with three separate paths:

1. Existing manual Binance commands (`PRICE`, `ANALYZE`, `CHART`, `ALERT`, `SEARCH`).
2. A new deterministic MEXC Futures market scanner that uses closed candles and sends WhatsApp trade signals when the configured confluence/risk gates pass.
3. A separately isolated MEXC Futures execution adapter. **Live execution is disabled in this build.**

## Current architecture

```text
WhatsApp Cloud API
        |
        v
   app.main webhook
        |
        v
      app.bot
   /     |      \
PRICE  ANALYZE  CHART/ALERT
  |        |
Binance  Binance

MEXC automation (independent)

MEXC Futures market data
        |
        v
   Universe selector
        |
        v
   4H / 1H / 15M
        |
        v
   Deterministic analysis
        |
        v
   Setup filter + RR
        |
        v
   Signal validator
        |
        +----> SQLite signal state + WhatsApp
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

The scanner analyzes MEXC USDT-settled perpetual contracts that are live and API-allowed. It ranks the available universe by 24h turnover when ticker data is available and then scans up to `MAX_SYMBOLS`.

The strategy gate is deterministic:

- 4H trend
- 1H market structure
- 15M BOS
- EMA 21/50
- RSI directional zone
- optional volume confirmation
- support/resistance sanity check
- ATR-derived SL/TP
- minimum confluence
- minimum risk/reward

Only fully closed candles are passed to the scanner analysis. The current/open candle is discarded when its interval has not finished yet.

The engine does not generate or claim a confidence percentage, guaranteed accuracy, or guaranteed profitability.

## Automatic WhatsApp signals

Set:

```text
SCANNER_ENABLED=true
AUTO_SIGNAL_ENABLED=true
AUTO_TRADE_ENABLED=false
```

Then configure either:

```text
AUTO_SIGNAL_RECIPIENTS=923xxxxxxxxx,923yyyyyyyyy
```

or, when that is blank, `ALLOWED_USERS` is used as the recipient set.

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

Confluence
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

Existing manual functionality remains Binance-based and is intentionally separate from MEXC automation.

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
