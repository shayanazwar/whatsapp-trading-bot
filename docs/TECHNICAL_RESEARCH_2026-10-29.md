# Technical research note — 29 October 2026

This note records the current official-documentation review for the backtest-first release. Documentation is a source reference; API reachability and the user's own account access have not been tested from the local runtime.

## MEXC Futures

- **Current REST host:** `https://api.mexc.com`. MEXC's change log announced the change from the older Contract host in January 2026.
- **Public 1-minute candles:** `GET /api/v1/contract/kline/{symbol}`, interval `Min1`; published max page size is 2,000 candles and the documented request limit is 20 requests per 2 seconds. This project derives completed 3-minute candles only from three contiguous, completed, epoch-aligned 1-minute candles. MEXC's documented native intervals do not include 3 minutes.
- **Contract metadata:** `GET /api/v1/contract/detail/country`. Fields documented include contract size, volume unit, min/max volume, price and volume scale, max leverage (including country/account field where returned), fees, maintenance-margin rate and state.
- **Ticker stream:** `wss://contract.mexc.com/edge`; public `sub.tickers` subscription provides all-contract ticker updates without authentication. Live ticker connection behavior is not verified in this execution environment.
- **Order docs reviewed for future implementation:** `POST /api/v1/private/order/create`, market type, side values for open/close long, isolated/cross open type, and the separate stop-order endpoint are documented. A private order client is deliberately not included in this release; no signed orders were sent.
- **Private request signing:** documented request headers include `ApiKey`, `Request-Time` and `Signature` (and optional receive window). The documented signature uses HMAC-SHA256 over the access key, timestamp and serialized parameter string; GET/DELETE parameters and POST JSON serialization must exactly match documentation. This was researched but not live-tested against an authenticated account.
- **API Futures fees:** the official announcement states maker 0.06% and taker 0.08% for API Futures fees effective 1 June 2026; eligible contract and regional/account exceptions apply. The backtester defaults to 0.08% taker fee per side as a **configurable assumption**, not proof of the user's account-specific rate.

Official references:
- https://www.mexc.com/api-docs/futures/integration-guide
- https://www.mexc.com/announcements/api-updates
- https://www.mexc.com/mexc-api
- https://www.mexc.com/api-docs/futures/market-endpoints/get-candlestick-data
- https://www.mexc.com/api-docs/futures/market-endpoints/get-contract-info
- https://www.mexc.com/api-docs/futures/market-endpoints/websocket-market-streams
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-stop-order
- https://www.mexc.com/announcements/article/updates-to-api-futures-trading-fees-jun-1-2026-17827791535742

## WhatsApp Cloud API

The policy page prohibits use of WhatsApp Business Services to buy, sell, promote or facilitate transactions in prohibited regulated goods, including real, virtual or fake currency. Therefore this build does not transmit crypto trading signals, crypto position/balance information or market charts through WhatsApp Cloud API. The policy gate remains false in deployment configuration. This must not be bypassed through an undocumented flag. Webhook verification, sender authorization and deduplication remain relevant for the safe support-only commands.

Official references:
- https://business.whatsapp.com/policy
- https://developers.facebook.com/docs/whatsapp/cloud-api/overview
- https://www.postman.com/meta/whatsapp-business-platform/collection/wlk6lh4/whatsapp-cloud-api

## Render and PostgreSQL

The Blueprint separates the HTTP web service from the long-running scanner worker and uses shared PostgreSQL. The HTTP service must bind `0.0.0.0:$PORT`; background workers do not receive public inbound HTTP traffic. A Postgres advisory lock guards against duplicate scanner workers. Plan/resource costs must be reviewed in the Render dashboard before deployment.

Official references:
- https://render.com/docs/web-services
- https://render.com/docs/background-workers
- https://render.com/docs/configure-environment-variables
- https://render.com/docs/health-checks
- https://render.com/docs/blueprint-spec
- https://render.com/docs/postgresql-creating-connecting

## Unresolved before live use

1. API network access, signatures, endpoint payloads and WebSocket reconnect behavior have not been tested against MEXC from this environment.
2. MEXC account region eligibility, KYC, Futures API permission, account collateral, one-way/hedge mode, contract-specific leverage and volume rules are not known.
3. Exact liquidation price and fee treatment need actual contract/account risk-tier data; the backtest uses an explicitly approximate liquidation model.
4. Historical depth and spread replay are not available from candle-only data; slippage is an estimate.
5. Meta policy approval for crypto-related WhatsApp messaging was not established; the channel remains blocked for that purpose.
6. Render deployment and PostgreSQL connection were not executed here.
