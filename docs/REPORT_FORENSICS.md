# Forensic Decision Register — v0.4.0

| Topic | Current decision | Evidence and limitation |
|---|---|---|
| Exchange | MEXC USDT-margined perpetual Futures only | Official host documented as `https://api.mexc.com`; runtime connectivity failed at DNS in this environment. |
| Strategy interval | Completed 3M bars derived from complete 1M bars | Tested on deterministic fixtures; real candle semantics still require a successful live-data probe. |
| Entry rules | RSI >= 70 and rising, EMA21/50 crossover, EMA21 > EMA50 > EMA100, price breakout and volume check; LONG only | 94 automated tests pass; this is not evidence of a profitable edge. |
| Contract universe | Public all-ticker stream plus contract metadata filter | Live WebSocket stream not verified in this environment. Missing state/sizing/leverage metadata is rejected. |
| Backtest | $100 starting equity, $1 initial margin, 1D/7D/30D/90D, API taker-fee assumption and adverse slippage | The engine and report generation are tested; no actual MEXC historical results are available because the runtime could not reach the API. |
| P&L costs | 0.08% API Futures taker fee per side used as a configurable default; funding and slippage disclosed | Fee schedule announced for API Futures effective 1 June 2026; regional/account/contract exceptions remain possible. Account-specific rate unverified. |
| Liquidation/stop | Conservative liquidation estimate checked before requested stop | Actual maintenance tiers, liquidation fee and exact exchange behavior are not verified; requested $3 stop is not a maximum-loss guarantee on $1 margin. |
| WhatsApp | Support-only HELP/STATUS while crypto messaging gate is closed | No crypto trade alerts, charts or balance messages through WhatsApp Cloud API; no production Meta delivery test. |
| Deployment | Render web + worker + PostgreSQL Blueprint | YAML parses; not deployed and PostgreSQL was not live-tested. |
| Live execution | Not implemented/released; all live flags false | No private order or account-write request was sent. |

## Verification snapshot

- `python -m compileall -q app tests`: passed.
- `pytest -q`: **94 passed**.
- Local FastAPI smoke: `/healthz` and `/dashboard` each returned HTTP 200 with application lifespan enabled.
- `render.yaml`: parsed as one web service, one worker and one PostgreSQL database.
- Archive integrity and secret-file check: passed.
- Ruff: not available; no lint pass is claimed.
- Actual MEXC history, live WebSocket, PostgreSQL, Meta delivery, Render deployment, and signed private trading: not verified.
