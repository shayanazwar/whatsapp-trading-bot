# Implementation Status — v0.4.0

## State at packaging

This is a backtest-first rebuild for the user-approved **MEXC USDT-M LONG-only, 3-minute RSI/EMA strategy**. Live order execution remains disabled and is not implemented/released in this build.

| Area | Current state | Evidence / limits |
|---|---|---|
| Public MEXC REST client | Implemented against current documented host, with timeouts, bounded retries, throttling, public ticker/candle/contract/funding reads | Mocked tests pass. Actual runtime connection failed at DNS resolution; payloads and account access remain unverified. |
| 3-minute candles | Derived from three contiguous, completed, epoch-aligned 1-minute candles | Unit tested; no incomplete candle should generate a decision. |
| Strategy | RSI(14) >= 70 and rising; recent EMA21/EMA50 cross; EMA21 > EMA50 > EMA100; close above EMA21 and prior high; volume confirmation; LONG only | Deterministic strategy fixtures pass. This is a hypothesis, not evidence of profitability. |
| All-contract candidate scan | Public all-ticker WebSocket + active-contract metadata; per-contract REST history checks | Implemented; WebSocket has not been verified against live MEXC in this execution environment. |
| Paper execution | Optional simulated entries/exits, one global position, contract quantity constraints, fees, slippage and estimated liquidation checks | Unit tests pass; simulated fills are not exchange fills. `PAPER_TRADING_ENABLED=false` by default. |
| Historical backtest | 1D/7D/30D/90D, $100 start, $1 margin, fee/slippage/funding accounting, quantity filters, CSV/JSON/text/equity chart | Engine and fixtures tested. No verified real-market results are available because MEXC history could not be downloaded from this environment. |
| Charts | 5M, 15M, 1H, 4H, 8H, 12H, 1D, 3D, 1W, 1MO; EMA 21/50/100/200; RSI with confirmed-pivot divergence markers | Fixture tests pass; current-price charts require live MEXC data access. |
| Dashboard | Token-protected `/dashboard`, command API and artifact endpoints; `/healthz` | Local smoke test returned HTTP 200 for dashboard and health endpoint. |
| WhatsApp | Signature/auth/deduplication code retained; policy gate closed for crypto research/alerts | Only support HELP/STATUS path; crypto trade notifications/charts/balance messaging are not enabled due Meta policy restriction. No live Meta delivery tested. |
| Persistence/deployment | Render web + background worker + PostgreSQL Blueprint; advisory lock prevents a second DB-connected worker | Blueprint YAML parsed; no Render or PostgreSQL instance created or tested. |
| Authenticated MEXC order execution | Not included/released | No signed private order was sent. No claims of live trading readiness. |

## Verification performed

- `python -m compileall -q app tests` — passed.
- `pytest -q` — **94 passed**.
- Render Blueprint YAML parsed successfully; it declares one web service, one background worker and one PostgreSQL database.
- Local FastAPI smoke test with lifespan enabled: `/healthz` HTTP 200, `/dashboard` HTTP 200; live switches both false.
- `ruff check app tests` — not run successfully because Ruff was unavailable in the execution environment.
- MEXC live API/network probe — failed with a DNS resolution error in this environment; this does not prove the exchange is down.
- WebSocket handshake, live Meta delivery, real PostgreSQL behavior, Render deployment, account permissions, and authenticated trading — not tested.

## Safety decisions

- `LIVE_EXECUTION_ENABLED=false` and `LIVE_EXECUTION_BUILD_RELEASED=false`.
- The $3 requested stop is not a guaranteed maximum loss with $1 margin; liquidation can be reached first. Liquidation in the simulator is an approximation.
- A no-data backtest is labelled **NOT RUN**, not reported as a flat/zero-return result.
- WhatsApp crypto trading messages remain blocked. Use the protected dashboard for charts and reports.
