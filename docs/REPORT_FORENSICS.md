# Backtest Report Forensics — v0.8.1

## Active report path

`BACKTEST` parses to S1-RFPC and calls `CommandProcessor._run_backtest`. Jobs are persisted by the web process and executed by the dedicated worker. The inactive `_run_rsi_ema_backtest` implementation is legacy and is not the active route.

## Reading results correctly

- Symbols requested/loaded/eligible and per-timeframe candle counts describe data availability, not profitability.
- Candidates, entry attempts and rejected entries are distinct counts.
- TP/SL counts are simulated exits on the chosen 1H OHLC series; same-bar ambiguity follows the conservative simulator rule.
- Open positions at test end are reported separately and are not counted as realized exits.
- Fees, slippage and funding are modeled/available-data adjustments. Missing funding is surfaced as a warning; it is not fabricated as zero.
- Max concurrent positions is an observed maximum; the configured 50-position cap does not guarantee 50 entries.
- Top-100 current-universe history is subject to survivorship/selection bias.

## Known model limitations

No historical order-book replay, queue priority, market impact, user external positions/orders or full exchange cross-margin liquidation engine is simulated. Current contract metadata and universe selection are not point-in-time historical snapshots. Reports must be treated as research estimates, not a 100% accurate reproduction of exchange execution.

## Required report audit checks

Every run should preserve its time window, universe, strategy version, completed-bar data coverage, requested/loaded symbols, bar counts, data errors, fees, slippage, funding coverage, open positions, TP/SL outcomes, rejection counts, config and report artifact paths. Missing or inconsistent fields should be warnings/errors, never silently synthesized.


The active command processor and scanner no longer import the legacy 3M RSI/EMA or MTF-TPR implementation at application startup. A shared market-precision helper is used by the S1-RFPC scanner; the old RSI/EMA backtester keeps a backward-compatible alias for its standalone historical tests.
