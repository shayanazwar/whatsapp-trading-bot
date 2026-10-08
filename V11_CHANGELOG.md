
## V11.1 Backtest/Trigger Fix

- Fixed 1H liquidity sweep detection to use a wick breach of the prior-three-candle liquidity level, followed by a separate subsequent reclaim candle.
- Preserved the strict V11 rule that the reclaim must occur on a later 1H candle and within the trigger window.
- Added side-specific rejection diagnostics so backtests expose the actual gate causing zero candidates instead of only reporting `technical_reject`.
- Added runner per-symbol `top_reject` logging.
- Added regression coverage for wick-only sweep + later reclaim behavior.

# V11 CHANGELOG

## Strategy layer
- Replaced V10 breakout/departure/retest/reclaim strategy with Trend Pullback / Value Re-entry.
- Added 4H impulse detection with 3/3 causal pivots.
- Added 4H retracement + EMA21/EMA50 value zone.
- Added multiple active impulse candidate evaluation instead of latest-only BOS selection.
- Added causal 1H liquidity sweep + reclaim trigger.
- Removed market-vs-limit setup complexity from the V11 baseline; execution is next 1H open.
- Rebuilt structural stop around actual pullback/sweep invalidation with ATR as a buffer only.
- Rebuilt target selection to choose the nearest structural target before RR evaluation.
- Lowered the post-cost RR floor to 1.60R in line with the research design; profitability is not assumed.
- Converted score and confirmation-family information to diagnostics/supporting evidence rather than accuracy gates.
- Preserved a hard abnormal-volatility shock veto.
- Preserved causal 1D/12H/4H/1H architecture.

## Validation/integration
- Updated setup filter to stop treating the V11 score as an accuracy gate.
- Updated risk manager defaults to V11's 1.60R floor and wider structural-stop sanity bounds.
- Updated configuration defaults and `.env.example`.
- Updated signal-key identity from BOS-specific naming to generic setup identity.
- Preserved legacy field names required by database, scanner and WhatsApp integrations.

## Important
The Claude/GPT/Gemini accuracy, PF and expectancy figures were research projections only. V11 makes no performance claim until backtested on sufficiently long out-of-sample data.

## Forensic fixes applied in Fixed V11
- Live signal freshness is capped at 5 minutes from the completed 1H close; stale scans are rejected.
- Live execution payload is MARKET (`type=5`) with no limit price; real execution remains disabled until full post-fill protection/reconciliation exists.
- 1H liquidity sweep and reclaim are separate candles; same-candle reclaim no longer qualifies.
- 4H impulse records now preserve semantic high/low indexes and require true HH/HL or LH/LL structure.
- Short-side structural target selection now chooses the nearest valid downside level.
- Target path is independently checked against confirmed 4H/12H/1D obstacles.
- Backtest reports expose both signal-close RR and actual next-open fill RR.
- Live and backtest post-cost RR use the same deterministic round-trip cost model.
- Removed obsolete duplicate V10 strategy definitions from the V11 engine.
- Added 180D and 365D paper-backtest windows.

## V11-balanced over-filtering corrections

- Replaced the all-components 1D macro hard gate with a directional 3-of-5 macro vote model using price vs EMA200, EMA50 vs EMA200, 3/3 structure, EMA50 slope and ADX.
- Corrected pullback invalidation to rely on completed 4H closes against the 4H impulse origin; 1H wick probes no longer falsely erase the setup.
- Corrected the liquidity trigger so the reclaim can occur on any subsequent 1H candle within the full six-candle window.
- Preserved the 1D/12H/4H/1H-only architecture, strict 4H HH/HL or LH/LL impulse structure, structural stop, structural-first target selection, post-cost RR, and next-1H-open MARKET execution.
