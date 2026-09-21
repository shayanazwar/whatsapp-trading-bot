# WhatsApp Trading Alert Bot

A WhatsApp-first cryptocurrency assistant. It accepts commands in WhatsApp, stores one-shot price alerts in SQLite, monitors Binance spot prices in real time, generates candlestick chart PNGs, and can search symbols across several public exchange market APIs.

## Features in this first version

- WhatsApp Cloud API webhook with signature verification.
- `HELP`, `PRICE`, `CHART`, `ALERT`, `ALERTS`, `DELETE`, `SEARCH` commands.
- One-shot `ABOVE` / `BELOW` alerts.
- Real-time Binance spot price cache using Binance's all-market mini-ticker WebSocket stream.
- Chart images for `5m`, `15m`, `1h`, `4h`, and `1d`.
- Candles + volume + EMA 21 + EMA 50.
- Symbol search across Binance, Bybit, OKX, Gate.io, and KuCoin (when their public market endpoints are available).
- SQLite persistence; data survives server restarts.
- Docker deployment support.

## Important scope

This version uses **Binance spot market data for real-time price alerts**. Symbol discovery can use additional public exchange endpoints. It does **not** claim to contain every TradingView symbol; TradingView aggregates data from many venues, and this bot uses public market-data sources that you explicitly configure.

Do not use this bot for automated trading. It only reads public market data and sends alerts/charts.

## WhatsApp commands

```text
HELP
PRICE BTCUSDT
CHART BTCUSDT 1H
CHART BINANCE:BTCUSDT 4H
SEARCH BTC
ALERT BTCUSDT ABOVE 120000
ALERT BTCUSDT BELOW 110000
ALERT BINANCE:ETHUSDT ABOVE 4500
ALERTS
DELETE 12
DELETE ALL
SEARCH PEPE
```

Accepted chart timeframes: `5M`, `15M`, `1H`, `4H`, `1D`.

Examples:

```text
ALERT BTCUSDT ABOVE 120000
```

creates a one-shot alert. It triggers only when price crosses from below the target to at/above the target. The inverse applies to `BELOW`.

## What you need from Meta

1. A Meta developer account.
2. A Meta app with the WhatsApp product added.
3. A WhatsApp Business phone number / Phone Number ID suitable for Cloud API use.
4. A System User access token with the required WhatsApp permissions.
5. Your Meta App Secret.
6. A public HTTPS URL for this service.

### Webhook URL

After deployment, your webhook callback URL is:

```text
https://YOUR-DOMAIN.example.com/webhook
```

Set the Verify Token in Meta to the same value as `META_VERIFY_TOKEN`.

Subscribe the WhatsApp webhook to the `messages` field.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Windows activation:

```powershell
.venv\Scripts\activate
```

The health endpoint should return:

```text
GET http://127.0.0.1:8000/health
```

## Android-only workflow

You can edit and deploy this project from an Android phone using GitHub + a cloud IDE/host that supports Python Docker deployments. No laptop is required once the project is deployed.

## Production deployment

Use a service that keeps a Python web process alive continuously. A sleeping service can delay price alerts.

Set all `.env.example` values as encrypted environment variables in the hosting platform.

Start command without Docker:

```text
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Docker command is already included in the `Dockerfile`.

## Security notes

- Never commit `.env` or access tokens.
- Set `ALLOWED_USERS` to your own WhatsApp number(s) if this is a private bot.
- Keep `META_APP_SECRET` set so webhook signatures are verified.
- The bot never needs a Binance API key for the public spot-market data it uses.
- The bot does not place trades and should not be given exchange trading credentials.

## Troubleshooting

### WhatsApp verification fails

- The server must be publicly reachable over HTTPS.
- `META_VERIFY_TOKEN` must exactly match the value entered in Meta.
- `/webhook` must answer the verification GET request with the challenge.

### Messages arrive but nothing happens

Check server logs. Make sure `META_ACCESS_TOKEN` and `META_PHONE_NUMBER_ID` are correct, and that the Meta app is subscribed to the WhatsApp `messages` webhook field.

### Alerts are late

The Binance stream updates the all-market mini-ticker stream about once per second. Network and hosting latency can add delay. This is an alerting tool, not an exchange-grade execution system.

### Chart fails for a symbol

The symbol may not exist on the selected exchange or the exchange may not expose the requested timeframe through its public OHLCV API. Try `SEARCH <name>` and select a returned market.

## Project structure

```text
app/
  __init__.py
  alerts.py
  bot.py
  charts.py
  config.py
  database.py
  main.py
  market.py
  whatsapp.py

tests/
  test_alerts.py
  test_bot_commands.py
  test_charts.py
```

## Data sources

- Binance public spot market-data REST/WebSocket endpoints for the real-time alert path.
- Public exchange market endpoints for multi-exchange symbol discovery.
- Charts are rendered by this application as PNG images.

The project is not affiliated with Meta, Binance, TradingView, or CCXT.
