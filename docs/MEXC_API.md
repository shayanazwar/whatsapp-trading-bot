# MEXC Futures market data

Base URL: `https://api.mexc.com` (the previous `contract.mexc.com` domain was retired in January 2026).

The adapter is intended for public Futures endpoints only:

- Contract specifications: `/api/v1/contract/detail/country`.
- Ticker snapshot: `/api/v1/contract/ticker`.
- K-lines: `/api/v1/contract/kline/{symbol}`.
- Order-book depth: `/api/v1/contract/depth/{symbol}`.
- Fair price: `/api/v1/contract/fair_price/{symbol}`.
- Current funding: `/api/v1/contract/funding_rate/{symbol}`.
- Funding history: `/api/v1/contract/funding_rate/history`.

Do not confuse Futures endpoints with Spot endpoints. Confirm response fields against the current MEXC docs during deployment; exchange API documentation and schemas can change. The Kline adapter requests bounded time windows of fewer than 2,000 points. Documented Kline interval values include `Min60`, `Hour4` and `Day1`; 12H is constructed locally from three contiguous 4H records aligned to UTC boundaries.

The WebSocket adapter targets the official Futures endpoint `wss://contract.mexc.com/edge`, with subscription payloads, ping, reconnect and bounded backoff. Validate the latest channel names and payload schemas against the live docs before running it as a production source. REST remains the fallback for historical candles.

No private key, account permission or order-placement endpoint is needed for this research-only release. MEXC Futures does not provide a public sandbox equivalent; paper fills are simulated locally and are not exchange-confirmed.
