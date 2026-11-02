# Release Notes — v0.8.1

This is a complete repository release, not a replacement-files-only patch. It consolidates the active S1-RFPC architecture and addresses worker observability/configuration consistency.

Changes: split queue polling and market scanning into independent asyncio loops (default queue poll 5 seconds); added early backtest start/time/universe logs; improved queue claim failure visibility; added worker health and non-secret database identity diagnostics; documented the actual lack of token fundamentals; added an explicit `NOT_IMPLEMENTED` fundamentals state to `ANALYZE`; aligned `.env.example`, Render config and current docs; moved contract tick rounding to a shared market module; removed unreachable legacy strategy imports from active command startup; omitted legacy Render plan overrides to preserve existing resource plans; adjusted ticker request spacing conservatively.

Safety: research backtest cap remains up to 50; paper concurrency remains one; paper/auto/live execution remain disabled; WhatsApp crypto delivery remains behind written-clearance configuration.

This release does not claim 100% backtest accuracy, does not implement full token fundamentals and does not implement live MEXC order placement.
