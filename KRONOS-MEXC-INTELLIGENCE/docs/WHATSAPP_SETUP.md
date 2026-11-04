# WhatsApp Cloud API setup

The app exposes an inbound webhook and a command interface using configured WhatsApp Cloud API credentials.

## Render environment variables

Set these on the web service:

- `WHATSAPP_ENABLED=true`
- `WHATSAPP_ACCESS_TOKEN` — Cloud API access token.
- `WHATSAPP_PHONE_NUMBER_ID` — numeric ID of the sending phone number (not the displayed phone number and not the WABA ID).
- `WHATSAPP_VERIFY_TOKEN` — random string you choose; it must match the webhook verification token configured in your provider console.
- `WHATSAPP_APP_SECRET` — app secret used to validate `X-Hub-Signature-256`.
- `WHATSAPP_ALLOWED_RECIPIENTS` — comma-separated international phone numbers allowed to issue commands, e.g. `923001234567`.
- `PUBLIC_BASE_URL` — public HTTPS URL of this Render web service, needed for chart image links.

The market, compute and inference workers receive the outbound token, phone-number ID and enabled flag from the web service through `render.yaml`. The web service handles inbound webhooks and chart image hosting.

## Webhook endpoint

Callback URL:

`https://YOUR-SERVICE.onrender.com/api/whatsapp/webhook`

Subscribe to the `messages` webhook field. The GET verification handler checks `hub.mode`, `hub.verify_token` and returns `hub.challenge`. The POST handler validates the HMAC SHA-256 signature against the raw request body before queueing message processing.

## Commands

- `HELP`
- `PRICE BTCUSDT`
- `ANALYZE BTCUSDT`
- `CHART BTCUSDT 4H` (also `1H` or `1D`; sends a PNG)
- `BACKTEST 1D`
- `BACKTEST BTCUSDT 7D`
- `BACKTEST BTCUSDT 30D`
- `BACKTEST STATUS`
- `STATUS` or `STATUS JOB_ID`
- `SCAN`

`ANALYZE`, `BACKTEST` and `SCAN` create durable jobs. The inference/compute/market worker sends a completion or failure message to the command sender. `STATUS` only reveals jobs created by that sender. Duplicate inbound message IDs are ignored.

## Important operating notes

- Only numbers in `WHATSAPP_ALLOWED_RECIPIENTS` can issue commands. Do not leave the allowlist empty in a public deployment.
- `WHATSAPP_APP_SECRET` is required for inbound POST signature verification.
- `PUBLIC_BASE_URL` must be public HTTPS so the provider can retrieve chart PNGs.
- All research and backtests remain paper-only. This integration does not place MEXC orders.
- Passing mock tests does not prove that the provider account, webhook subscription, message recipient, token permissions, or live deployment is configured. Verify end-to-end after deployment and follow applicable account and platform rules.
