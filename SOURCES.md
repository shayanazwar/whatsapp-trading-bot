# Technical references checked — 2026-11-06

These references support API and hosting implementation details. No WhatsApp policy pages were consulted.

## MEXC Futures API

- [Futures API domain update](https://www.mexc.com/announcements/article/futures-api-access-domain-update-17827791532974) — MEXC announced `https://api.mexc.com` as the Futures API access domain in January 2026.
- [Get Candlestick Data](https://www.mexc.fm/api-docs/futures/market-endpoints/get-candlestick-data) — `GET /api/v1/contract/kline/{symbol}`, `Min1` supported, Unix-second start/end, 2,000 data points maximum per request, segmented requests needed for longer ranges.
- [Get Contract Info](https://www.mexc.fm/api-docs/futures/market-endpoints/get-contract-info) — `/api/v1/contract/detail/country`; documents contract size, tick, volume step, minimum volume, leverage fields, maintenance-margin and liquidation-fee metadata.
- [Get Ticker (Contract Market Data)](https://www.mexc.co/zh-MY/api-docs/futures/market-endpoints/get-ticker-contract-market-data) — documents `lastPrice`, bid/ask top-of-book, fair price and timestamp. This implementation uses last price only for stale-signal rejection, not for candle-based signal generation.
- [Get Fair Price Candles](https://www.mexc.io/api-docs/futures/market-endpoints/get-fair-price-candles) — fair-price Min1 history endpoint exists; this release does not use it to claim exact liquidation reconstruction.
- [Get Funding Rate History](https://www.mexc.fm/api-docs/futures/market-endpoints/get-funding-rate-history) — endpoint reference; cashflows are not applied in this build.

## WhatsApp technical API

- [Meta's official WhatsApp Business Platform Postman workspace](https://www.postman.com/meta/whatsapp-business-platform/overview) — official API collection and developer documentation entry point.
- [Meta Send Text Message request](https://www.postman.com/meta/whatsapp-business-platform/request/8gvd47s/send-text-message) — Cloud API message request endpoint example. This is API integration documentation, not a policy page.

The default `GRAPH_API_VERSION=v26.0` is configurable and must be confirmed as available to the owner's Meta app before deployment; no claim of account-specific version entitlement is made.

## Render

- [Free instance limitations](https://render.com/docs/free) — free web services spin down after 15 minutes of inactivity, use ephemeral local storage, and cannot attach persistent disks.
- [Persistent disks](https://render.com/docs/disks) — paid services can attach persistent storage.
- [Web services](https://render.com/docs/web-services) — service deployment and configuration reference.

The Blueprint uses a paid always-on web-service plan and a persistent disk. Current billing must be checked in the Render dashboard; no fixed price is stated here.
