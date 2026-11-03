# Architecture

## Services

1. **Web/API** — FastAPI, authenticated admin routes, React static assets, status and job APIs. HTTP handlers enqueue jobs and return; they do not run neural inference or long backtests inline.
2. **Market worker** — periodically obtains the active USDT-settled perpetual universe and queues candidate analysis. It does not submit exchange orders.
3. **Compute worker** — claims forecast, analysis and backtest jobs from PostgreSQL, uses bounded retries/leases and records progress/results.
4. **PostgreSQL** — durable job queue, signals, reports, forecasts and audit events; structured payloads use PostgreSQL JSONB (SQLite JSON for local tests). SQLite is suitable only for local single-process work.
5. **Forecast artifacts** — individual paths are compressed for storage in the current database implementation. There is no object-store adapter in this release. Monitor PostgreSQL growth and add a separately tested S3-compatible adapter before retaining large numbers of forecasts.

## Data flow

MEXC public REST/WebSocket → validation/UTC normalization → candle persistence → 1D/12H/4H/1H strategy → Kronos inference (1,000 individual paths) → independent historical bootstrap (10,000 sequences) → TP/SL barrier analysis → fee/slippage/funding and risk checks → paper research signal → authenticated dashboard feed.

## Reliability

Jobs carry a UUID, payload, state, progress, attempts, worker ID and lease expiry. Claiming uses a database transaction and row locking on PostgreSQL. Expired jobs are recovered. Retry attempts are bounded. HTTP timeouts and retry delays are finite. Worker process roles are separate so model work does not block market collection.

## Security boundary

The app uses a signed admin session cookie, a separate CSRF cookie/header pair, a configured admin password, server-side route dependencies and secure cookies in production. Use a long random `APP_SECRET_KEY`, a unique high-entropy admin password, HTTPS, and a private Render service environment. The app contains no private exchange order API.
