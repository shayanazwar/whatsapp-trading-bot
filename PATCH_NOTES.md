# Release Notes — v0.8.2 Forensic Repair

This is a complete repository replacement archive based on v0.8.1. The S1-RFPC strategy, signal rules, supported timeframes (1D → 12H → 4H → 1H), and research-only/live-execution-disabled architecture are preserved.

## Corrected defects

1. **Funding coverage:** the active backtest now calls the coverage-aware MEXC API. Funding histories are fetched and assessed per report window, partial/failed history is surfaced in structured result metadata and the WhatsApp report, and incomplete funding is never represented as complete.
2. **Market-data quality:** scanner and paper-entry paths now share a hard data/contract quality gate. Historical continuity checks cover 1D, 12H, 4H and 1H; gap, off-grid and duplicate open-time warnings reject new signals. Backtests measure exact window candle coverage, require continuity, validate contract metadata, and enforce the configured universe-coverage threshold before simulating a window.
3. **Risk controls:** daily realized loss and realized peak-to-current drawdown are derived from the paper ledger and passed into `RiskEngine`. The scope is explicitly realized-only; open-position mark-to-market and liquidation risk are not claimed. Simultaneous closes are aggregated before drawdown is computed.
4. **Funding at exit:** paper-close funding history is fetched through the exit candle close. Adverse settlements within an ambiguous exit candle are charged conservatively; favorable inside-candle settlements are not credited.
5. **WhatsApp recovery:** startup recovery uses stable keyset pagination in bounded pages, with no fixed 50-message ceiling. Duplicate webhook deliveries can redispatch a still-pending persisted message while an in-process guard prevents concurrent duplicate execution.
6. **Contract validation:** the active backtest now invokes a shared fail-closed USDT-perpetual contract validator before admitting a symbol.
7. **Backtest job status:** successful simulation results are persisted before report delivery. Notification failures are recorded separately and cannot downgrade a completed job or erase its result.
8. **Event timestamps / strategy edge cases:** intrabar exits use the candle-close resolution timestamp; gap and time/data-stop exits use the observed candle open. Incomplete-only 1H series return an explicit rejection instead of raising from `max()`. Backtest holding periods preserve fractional-hour precision.

## Added regression tests

`tests/test_forensic_fixes.py` covers candle completeness/continuity/off-grid detection, contract/data vetoes, conservative funding attribution, paper risk metrics and limit enforcement, multi-page inbound recovery, terminal backtest job state, incomplete 1H input, and event/holding-time precision.

## Local verification

- Automated tests: **158 passed**.
- Python byte-compilation: passed.
- Runtime import smoke tests: passed.
- Ruff: not run locally because the Ruff executable is not installed in this environment; the configured GitHub Actions lint job remains required.
- Runtime used for this repair: Python 3.13.5. The project CI/Docker target is Python 3.12; Python 3.12 execution was not available in this environment.
- Live MEXC, Render/PostgreSQL and Meta WhatsApp integrations were not executed.

## Safety / remaining limits

This is still a research and paper-simulation build, not live-trading-ready. Live order execution remains unavailable/disabled. Risk drawdown and daily-loss metrics use realized paper-ledger P&L and exclude open-position mark-to-market. Paper/backtest fills, funding, fees, slippage, intrabar sequencing and liquidation are still modeled rather than exchange-confirmed. No token-fundamentals provider is implemented.
