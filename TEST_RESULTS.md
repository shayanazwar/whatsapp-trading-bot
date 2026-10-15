# Test Results — V11 Research Patch

## Executed successfully

- Full suite: `python -m pytest -q` — **102 passed, 0 failed, 0 skipped**.
- Compilation: `python -m compileall -q app tests` — passed.
- Imports: `import app.main` — passed.
- Timeframe contract: `APPROVED_TIMEFRAMES == ('1D', '12H', '4H', '1H')` — passed.
- Default config: `V11_ACCURACY_MODE` absent/false returns `enabled=False`, max reclaim 3 bars, max extension 0.75 ATR, max retest depth 0.70.
- Execution safety config: `scanner_enabled=False`, `auto_signal_enabled=False`, `auto_trade_enabled=False`, `allow_live_execution=False` under `Settings()` defaults.
- ZIP contents/integrity: verified after packaging; only changed/new source, tests, and documentation are included.

## Newly covered behavior

- The reclaim-window limit is measured from each sweep, not from the first value-zone touch.
- A qualifying reclaim on the current completed 1H candle is not masked by an older historical reclaim.
- ADX is not counted as a directional vote.
- Accuracy filters only reject entries when `V11_ACCURACY_MODE=true`; they are disabled by default.
- The backtest carries measured entry features to trade outcomes and summarizes available TP vs SL feature averages.
- Actual-fill extension is measured only when the swept level and 1H ATR are available.

## Not performed / not claimed

- No MEXC historical backtest was run: the ZIP does not include a frozen historical candle dataset or matching historical universe snapshot, and this environment cannot establish reproducible performance from those inputs.
- No predictive accuracy, expectancy, win-rate, or profitability improvement is claimed.
- A complete FastAPI lifespan/startup requiring deployment credentials was not performed. No secrets were added or exposed.

The 102 passing tests establish local code/test correctness for covered cases; they do not prove a profitable trading edge or production readiness.
