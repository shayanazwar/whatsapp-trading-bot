# Release Notes — v0.8.4 Audit Follow-up

## Scope

This patch follows the v0.8.3 S1-RFPC research release. It preserves the active completed-candle strategy:

`1D → 12H → 4H → 1H`

Live order execution remains disabled. Legacy 3M RSI/EMA and MTF-TPR modules remain inactive for the active `SCAN` and `BACKTEST` path.

## Confirmed fixes

- MAE/MFE no longer use the full 1H candle high/low when a position exits during that candle. Exit bars now record only the modeled exit price because OHLC data cannot reveal the intrabar path after the exit. Full candle excursions are used only when the position survives the complete candle. This is conservative and may understate excursions on exit bars, but avoids recording post-exit prices.
- When a symbol has no candle at a global portfolio timestamp, the Cross Margin stress proxy now uses its last known close rather than replaying the previous candle's old high/low extremes. Forced portfolio liquidation for an absent symbol uses a synthetic mark candle at the current global timestamp, keeping funding accrual and exit timestamps aligned.
- Liquidation at the next candle open and liquidation inferred from intrabar OHLC stress now have separate exit reasons/timestamps. Intrabar OHLC stress is no longer timestamped falsely at the candle open.
- Funding coverage is now reconciled against the actual final funded run's closed and open positions, not only the preflight run. A symbol that appears only after the funding rerun is marked incomplete and the report is not marked funding-complete.
- Help text now matches the configured 25× default gross-notional cap.
- Package metadata, environment sample and runtime build version are synchronized to 0.8.4.
- README and test report now distinguish the conservative liquidation stress proxy from exchange-exact liquidation and accurately describe mark-to-market open positions.

## Remaining limitations

- The Cross Margin engine remains a research approximation. It does not replay historical Fair Price, the user's external cross positions/open orders, exchange partial liquidation, ADL or historical order-book impact.
- Funding completeness is now reported against the final portfolio. If a newly entered symbol was not fetched during preflight, the result is marked incomplete; this patch does not silently assume zero funding or automatically perform unlimited refetch loops.
- Current top-turnover universe snapshots still create survivorship bias for historical periods; the selected symbols and snapshot time are recorded, but this is not a point-in-time historical universe.
- Top-universe command scope and single-symbol extended-window restrictions are unchanged.
- No live MEXC, production PostgreSQL, Render deployment or Meta account test was run.

## Local validation

- `pytest -q`: 174 passed.
- `python -m compileall -q app tests`: passed.
- Render YAML parse and `git diff --check`: passed.
- Python used: 3.13.5. Docker targets 3.12; that runtime was not independently tested here.
- Ruff was unavailable in the local execution environment; CI lint remains required.
