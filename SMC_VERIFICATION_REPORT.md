# SMC-1.0 Verification Report

## Verdict

- **Code/test status:** Passed the local automated suite and Python import/compile checks listed below.
- **Historical performance:** Not verified. No live MEXC historical backtest completed in this execution environment because `api.mexc.com` DNS resolution failed (`Temporary failure in name resolution`).
- **Outperformance vs V11/control:** Unknown. No win rate, expectancy, profit factor, or P&L figures are reported because no valid historical results were produced.
- **Live execution:** `AUTO_TRADE_ENABLED=false` and `ALLOW_LIVE_EXECUTION=false` remain the defaults; both were confirmed false during verification.
- **Readiness:** Research/paper evaluation only; not production-ready on the evidence currently available.

## Architecture and changes

The project remains a WhatsApp/FastAPI bot. The existing V11 analysis engine, MEXC candle client, symbol universe, signal validation, simulator, report generation, and WhatsApp delivery are retained. New strategy modules plug into the existing scanner/backtest analyzer interface. `ANALYSIS_ENGINE` selects the runtime scanner engine (`V11` by default or `SMC`). `BACKTEST_ENGINE` can independently select `V11`, `SMC`, or `CONTROL` for a historical comparison and does not alter scanner selection.

The SMC engine uses completed 1D, 12H, 4H, and 1H candles only. Its objective rules and grouped SMC concept matrix are in `SMC_ENGINE_SPEC.md`. The control comparator is deliberately simpler: 1D EMA50/EMA200 bias, 4H EMA21/EMA50 alignment, and a completed 1H three-bar breakout. It is available only to the backtest runner.

## Fixes included

1. Added an isolated SMC candle-analysis module with causal confirmed swings, structural context, dealing range/premium-discount, OB/FVG proxies, liquidity sweep then later reclaim, structural SL/target geometry, and post-cost RR eligibility.
2. Routed scanner/backtest calls through the new SMC analyzer while leaving V11 as the default.
3. Added the backtest-only CONTROL comparator for fair future A/B testing.
4. Added the missing 3D backtest choice.
5. Corrected the rolling backtest end boundary: it now ends at the latest UTC hour boundary rather than being shifted back by the 72-hour maximum hold horizon.
6. Added support for canonical `BACKTEST_START_MS` / `BACKTEST_END_MS`; legacy `V11_BACKTEST_START_MS` / `V11_BACKTEST_END_MS` remains supported. Canonical variables take precedence when both are set.
7. Prevented the current unfinished 1H candle's incomplete high/low/close from resolving historical simulated TP/SL outcomes. Such an open trade is retained as unresolved at the as-of time.
8. Kept live execution switches off and default scanner strategy on V11.

## Executed verification

Commands/results from the local project tree:

- `python -m compileall -q app tests` — passed.
- `pytest -q` — **92 passed** on the completed run (no test failures).
- `python` import check for `app.main` — passed; FastAPI application loaded.
- Settings check — `ANALYSIS_ENGINE=V11`, empty `BACKTEST_ENGINE`, and both live-trading switches false by default.
- Backtest-duration check — supports 1D, 3D, 7D, and 30D, along with existing longer intervals.
- Synthetic strategy tests — causal swing confirmation, long/short sweep-reclaim, full SMC LONG and mirrored SHORT geometry, 12H aggregation, cost/RR guard, rolling-window boundary, unfinished 1H candle safety, CONTROL fallback, and backtest environment-variable precedence.

## Required real-data backtests — not executed

| Window | V11 baseline | SMC-1.0 | CONTROL-1.0 | Reason |
|---|---|---|---|---|
| 1D | Not run | Not run | Not run | MEXC public hostname could not resolve in this environment |
| 3D | Not run | Not run | Not run | MEXC public hostname could not resolve in this environment |
| 7D | Not run | Not run | Not run | MEXC public hostname could not resolve in this environment |
| 30D | Not run | Not run | Not run | MEXC public hostname could not resolve in this environment |

These are **not zero-trade results**; no valid real-data run was completed. Do not interpret the synthetic tests as evidence of profitability. The actual comparative report must use the same frozen `BACKTEST_START_MS` / `BACKTEST_END_MS`, same eligible universe/data snapshot, and same fee/slippage assumptions for all three engines. Funding and historical spread/depth remain limitations unless the source data is available and timestamp-aligned.

## Changed/additional files

- `.env.example` — documents safe engine selectors and reproducible backtest timestamps.
- `app/analysis/smc_engine.py` — new independent SMC-1.0 engine.
- `app/analysis/control_engine.py` — new simple comparator, restricted to backtests.
- `app/automation/scanner.py` — optional SMC analyzer routing; V11 remains the default and CONTROL is not selectable in scanner.
- `app/backtest/runner.py` — SMC/CONTROL backtest routing, 3D support, true rolling period boundary, partial-candle outcome safety, and canonical backtest timestamp variables.
- `app/bot.py` — exposes the 3D backtest command.
- `app/config.py` — strategy selectors, including a backtest-only override.
- `tests/test_smc_engine.py` — synthetic strategy, aggregation, safety and period-selection tests.
- `SMC_ENGINE_SPEC.md` — strategy rules and grouped SMC concept matrix.
- `SMC_VERIFICATION_REPORT.md` — this evidence/limitation report.

## Most important next step

Deploy these replacement files with MEXC access available, then run `BACKTEST 1D`, `3D`, `7D`, and `30D` for `BACKTEST_ENGINE=V11`, `SMC`, and `CONTROL` against fixed identical windows. Keep both live switches false. Only after the comparable outputs exist can we judge whether SMC improves expectancy, profit factor, drawdown, and both-side performance.
