# MEXC Futures market data

Base URL: `https://api.mexc.com` (the previous `contract.mexc.com` domain was retired in January 2026).

The adapter is intended for public Futures endpoints only:

- Contract specifications: `/api/v1/contract/detail/country` (optional `symbol` query parameter; this is the current official Get Contract Info route and returns the contract precision, fee and risk fields used by this application). MEXC also references `/api/v1/contract/detail` in its API overview/demo code; do not substitute it without verifying that its response carries the same current country-specific precision/risk metadata.
- Ticker snapshot: `/api/v1/contract/ticker`.
- K-lines: `/api/v1/contract/kline/{symbol}`.
- Order-book depth: `/api/v1/contract/depth/{symbol}`. Current docs describe `asks` and `bids` levels as numeric arrays `[price, order_count, order_quantity]`; the liquidity gate uses the third field as contract quantity, multiplied by contract size and price to estimate quote notional.
- Fair price: `/api/v1/contract/fair_price/{symbol}`.
- Current funding: `/api/v1/contract/funding_rate/{symbol}`.
- Funding history: `/api/v1/contract/funding_rate/history`.

Do not confuse Futures endpoints with Spot endpoints. Confirm response fields against the current MEXC docs during deployment; exchange API documentation and schemas can change. The Kline adapter requests bounded time windows of fewer than 2,000 points. Documented Kline interval values include `Min60`, `Hour4` and `Day1`; 12H is constructed locally from three contiguous 4H records aligned to UTC boundaries.

The WebSocket adapter targets the official Futures endpoint `wss://contract.mexc.com/edge`, with subscription payloads, an independent 15-second ping task (not dependent on ticker traffic), pong timeout, reconnect and bounded backoff. Validate the latest channel names and payload schemas against the live docs before running it as a production source. REST remains the fallback for historical candles.

No private key, account permission or order-placement endpoint is needed for this research-only release. MEXC Futures does not provide a public sandbox equivalent; paper fills are simulated locally and are not exchange-confirmed.
