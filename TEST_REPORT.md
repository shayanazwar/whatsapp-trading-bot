# KRONOS-MEXC-INTELLIGENCE — Corrected Build Verification Report

**Snapshot date:** 2026-11-03  
**Project version:** 1.0.2  
**Mode:** research and paper only; no live order execution.

## Fixes applied to the uploaded source

### P0 — trading-result and approval integrity

1. **Partial TP accounting:** TP1 and the remaining TP2/stop leg are accounted as separate quantities. A stop after TP1 subtracts the remaining leg's loss. Stop-market gaps fill at the adverse candle open.
2. **Consistent exit schedule:** The configured TP1 fraction and TP2 multiple are now used by net RR, Kronos barrier races, historical scenario races, backtesting, mark-to-market and funding exposure. Default is 50% at 1R and 50% at 2.5R (1.75R gross weighted reward before costs).
3. **Kronos approval logic:** Live analysis and Kronos-filtered backtests evaluate forecast paths against the actual candidate entry, stop, TP1 and TP2. Directional path share is descriptive only, not an approval probability.
4. **Fresh market revalidation:** Before approval, the app refreshes ticker and order book; rejects stale/future timestamps, excessive spread/drift, already-crossed stop, already-reached original TP1/TP2 and re-runs both TP/SL races at refreshed levels.
5. **Margin/contract limits:** Sizing is bounded by stop risk, initial margin plus opening costs, leverage, min/max quantity, volume step and available contract risk tier. CUSTOM/INCREASE contracts are capped conservatively to documented first-tier/base limits. Aggregate paper risk, margin reservation and notional caps are enforced.
6. **Visible order-book capacity:** Uses current MEXC depth arrays `[price, order_count, contract_quantity]`, the correct third field for quantity, side-aware LONG asks / SHORT bids, contract size and a configurable price band. Insufficient displayed depth blocks paper approval. This is a snapshot gate, not a fill guarantee.
7. **Funding is not silently zero-filled:** Backtests fail closed if funding history is unavailable or a settlement cannot be valued. Event fair price is used when present; otherwise a labelled last-completed-1H-close proxy is applied. Funding exposure after TP1 uses the configured remaining fraction. Current signal analysis does not approve when the funding snapshot is unavailable; signal/race R values are explicitly before future funding.
8. **Portfolio `ALL` curve:** Equal sleeve aggregation now retains the allocated cash for failed symbol sleeves instead of reallocating failed sleeves' capital to the successful subset.

### P1 — data integrity and strategy correctness

- MEXC Kline arrays must all exist, have equal lengths and contain no null candle values. Empty pagination pages advance the cursor; retry policy only retries network/timeouts, HTTP 429 and 5xx, not bad 4xx requests.
- Candle series are strictly checked for duplicates, UTC alignment and interval continuity before indicator/forecast/backtest use. 12H is built only from three exact contiguous completed 4H candles.
- RSI handles monotonic-up, monotonic-down and flat windows correctly. Missing volume does not earn a positive confirmation.
- BOS is a crossing event rather than a persistent condition above a previously broken swing.
- Breakout/retest requires a prior completed breakout candle and a separate later retest candle.
- Backtest equity includes hourly mark-to-market and conservative intrabar adverse estimates. Unresolved trades remain separate from realized win/loss metrics.
- Candle history is persisted idempotently; forecast inputs and backtest datasets/configurations have SHA-256 fingerprints.
- MEXC WebSocket heartbeat is sent independently every 15 seconds, with pong timeout and bounded reconnect.
- Analysis retains missing funding/market fields as `null` rather than displaying fabricated zeros; valid zero funding is preserved.

## Checks completed in this environment

| Check | Result | Evidence / scope |
|---|---|---|
| Backend automated tests | **PASS** | `python -m pytest -q` — **83 passed** |
| Python byte compilation | **PASS** | `python -m compileall -q app scripts alembic` |
| Editable package install | **PASS** | `pip install -e . --no-deps --no-build-isolation` |
| Fresh SQLite Alembic migration | **PASS** | New temporary DB; migrations `0001_initial` and `0002_whatsapp_events` |
| Render Blueprint YAML | **PASS** | `yaml.safe_load`; 4 services and 1 database parsed |
| Frontend TS/TSX syntax | **PASS** | Global TypeScript transpiler; 4 `.ts`/`.tsx` source files, zero syntax diagnostics |
| MEXC contract depth parser | **PASS** | Offline tests assert documented third array field is contract quantity and LONG/SHORT side selection |
| Funding exposure | **PASS** | Offline test verifies configured partial TP1 fraction controls post-TP1 funding exposure |
| API/login/status and safety flags | **PASS** | Covered by automated FastAPI and startup-safety tests |

## Not verified in this environment — do not interpret as passing

| Check | Status | Reason |
|---|---|---|
| Full frontend `npm install` + `npm run build` | NOT RUN | npm registry access unavailable; only TS/TSX syntax transpilation completed |
| Docker image builds | NOT RUN | Docker CLI/daemon is not installed |
| Live MEXC REST/WebSocket | NOT RUN | No outbound exchange access in this execution environment; fixtures validate parser/contracts only |
| Real Kronos weights download / inference | NOT RUN | GitHub/Hugging Face download not available here; pinned adapter is not a real model benchmark |
| 1,000-path CPU latency / memory benchmark | NOT RUN | Requires actual model weights and target compute service |
| Render deployment | NOT DEPLOYED | No Render authorization/deployment action available |
| GitHub private repo creation/push | NOT CREATED | No repository-creation action available through the connected GitHub tool |
| Ruff / mypy | NOT RUN | Their CLI executables are not installed in this environment |
| WhatsApp Cloud API integration | **MOCK-TESTED** | Signed webhook, allowlist, deduplication, commands, PNG chart rendering, async job replies and bounded sender tests pass; no live account send verified |

## Remaining limitations

1. `BACKTEST ALL` uses independent equal-capital symbol sleeves. It is not a synchronized cross-margin portfolio and does not model portfolio correlation, account-wide maintenance-margin changes or liquidation across all positions. Failed symbol sleeves remain cash in the aggregate curve.
2. Current contract metadata is used when archived point-in-time fee/tick/risk-tier metadata is unavailable. Historical universe membership/survivorship bias is not reconstructed.
3. Historical OHLC backtests cannot recreate queue priority, full market impact, actual partial fills, tick-level intrabar sequence or all MEXC liquidation mechanics.
4. The funding rate used for the signal-time snapshot may change before settlement. Forecast and historical TP/SL race R is before future funding; historical backtests use observed funding events with fair-price values or an explicitly labelled 1H close proxy.
5. Kronos pretraining-data cutoff is not independently verified. Historical Kronos-filtered backtests are flagged as potentially overlapping the model's pretraining corpus, not leakage-cleared.
6. Kronos path shares are model sample frequencies, not calibrated real-world probabilities. Out-of-sample evaluations must accumulate across many independent forecast origins before calibration claims.
7. Signal outcome reconciliation against future candles and an automated realized daily-loss kill switch are not implemented.
8. Forecast arrays remain gzip-compressed in PostgreSQL. An S3-compatible artifact backend and automated retention/compaction job are not included.
9. WhatsApp Cloud API is implemented but still requires live token, phone-number ID, webhook subscription, app-secret, recipient allowlist and Render end-to-end verification.

## Deployment gate

Before describing this as operational: run the full npm build and Docker builds on a network-enabled runner; deploy the Render Blueprint; verify all services share the same PostgreSQL instance; confirm live MEXC REST/WebSocket payloads and reconnect behavior; download pinned Kronos source/weights; run an actual inference smoke test and benchmark 1,000 paths; download historical MEXC candles/funding and run a walk-forward backtest. Keep live order placement absent; complete WhatsApp Cloud API end-to-end verification before relying on command delivery.

## WhatsApp implementation checks

- `GET /api/whatsapp/webhook` verifies the configured challenge token.
- `POST /api/whatsapp/webhook` validates the raw-body HMAC signature before processing.
- Inbound phone numbers are checked against `WHATSAPP_ALLOWED_RECIPIENTS`.
- Message IDs are persisted to prevent duplicate command execution on webhook retries.
- `PRICE` uses the MEXC public Futures ticker adapter; `ANALYZE` uses the existing analysis job/inference worker; `CHART` renders a PNG and sends its HTTPS URL; `BACKTEST` supports 1D/7D/30D through the existing compute worker; `STATUS` is scoped to the sender's own queued jobs.
- The market/compute/inference Render workers receive the outbound WhatsApp settings from the web service.
- `83` automated tests pass, including mocked webhook, command, sender, chart, status and queue cases. No live Cloud API request was made from this environment.

**A passing offline suite is not evidence of profitability, live exchange correctness, live WhatsApp delivery or production readiness.**
