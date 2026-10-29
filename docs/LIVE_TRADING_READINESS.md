# Live-trading readiness — v0.4.0

**Status: NOT READY. Live trading is disabled.**

| Requirement | Status |
|---|---|
| User-approved strategy | Yes: 3M LONG-only RSI/EMA, under test |
| Historical backtest engine and CSV/JSON reports | Implemented; actual MEXC data retrieval not verified here |
| MEXC public market endpoints | Implemented against documented paths; live network test blocked by DNS in this environment |
| MEXC authenticated signing | Researched, not implemented/released or live-tested in this version |
| Private order/position client | Not implemented/released |
| Exchange-side TP/SL and reduce-only close | Not implemented/released or account-tested |
| Position/order reconciliation after timeouts | Not implemented for live orders |
| Account-specific fees, collateral, margin mode and leverage | Unverified |
| Contract-specific liquidation/risk tiers | Approximate only in backtest; needs verification |
| PostgreSQL on Render | Configured in Blueprint, not deployed/tested here |
| Single-worker guard | PostgreSQL advisory lock implemented; not tested with a deployed database |
| WhatsApp crypto trade notifications | Blocked due published policy restriction |
| Live-order switch | Disabled; no order path released |

Do not enable real-money trading on the basis of this build. Before considering live release, implement and audit signed private order/position methods, exchange-confirmed close and protective order semantics, idempotent order reconciliation, and a safe paper-trading lifecycle; then test with the user's eligible MEXC account using minimal non-trading/account-read checks and formal human approval.
