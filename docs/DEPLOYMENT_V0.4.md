# Deployment steps — v0.4.0

1. Create a private GitHub repository and push the project using the Git commands in the root README.
2. In Render, choose **New → Blueprint**, select the repository, and review each resource before creation. The Blueprint declares a web service, background worker and PostgreSQL database in Singapore.
3. Check the web service command binds to `0.0.0.0:$PORT` and health check is `/healthz`.
4. Set a strong `WEB_API_TOKEN` in Render. The generated `DATABASE_URL` is supplied to the web service and worker by the Blueprint.
5. Keep all trading switches false: `AUTO_TRADING_ENABLED`, `LIVE_EXECUTION_ENABLED`, and `LIVE_EXECUTION_BUILD_RELEASED`. The software in this release does not contain the released order-submission path.
6. Keep `WHATSAPP_CRYPTO_MESSAGING_ALLOWED=false`; do not configure crypto trading alerts through WhatsApp.
7. After both services start, check logs for the scanner's ticker stream, data freshness, scan summary and errors. If network or market data is stale, candidate scanning should skip.
8. Open `/dashboard`, enter `WEB_API_TOKEN`, run `BACKTEST 7D`, and check the JSON data coverage before using the performance results.

## Environment values

- `DATABASE_URL`: supplied by Render PostgreSQL. Use internally on Render; do not print it to logs.
- `WEB_API_TOKEN`: dashboard API access token; required to run web dashboard commands.
- `MEXC_REST_BASE_URL`: fixed to `https://api.mexc.com`.
- `MEXC_PRICE_SERIES`: `standard` uses standard OHLC; `real` requires real OHLC fields.
- `DEFAULT_SYMBOLS`: symbol list used by dashboard backtests; scanner universe is based on eligible contract metadata/ticker stream.
- `INITIAL_MARGIN_USDT`, `MAX_LEVERAGE`, `NET_TAKE_PROFIT_USDT`, `REQUESTED_STOP_LOSS_USDT`: simulation assumptions.
- `EXCHANGE_TAKER_FEE_RATE`, `BACKTEST_SLIPPAGE_BPS_PER_SIDE`: simulated cost assumptions.
- `AUTO_TRADING_ENABLED`, `LIVE_EXECUTION_ENABLED`, `LIVE_EXECUTION_BUILD_RELEASED`, `PAPER_TRADING_ENABLED`: leave false in this release.
- `WHATSAPP_CRYPTO_MESSAGING_ALLOWED`: leave false.
- `META_VERIFY_TOKEN`, `META_APP_SECRET`, `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`, `WHATSAPP_ALLOWED_SENDERS`: only for a permitted, safe support channel; never place actual values in source control.

The `render.yaml` service plan is a starting template, not a price guarantee. Confirm plans and current price in your Render account before applying the Blueprint.
