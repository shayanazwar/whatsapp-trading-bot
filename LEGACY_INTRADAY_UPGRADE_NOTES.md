# Intraday Upgrade v2.0

Core files changed in priority order:
1. `app/analysis/engine.py`
2. `app/automation/scanner.py`
3. `app/backtest/runner.py`
4. `app/backtest/simulator.py`
5. `app/backtest/report.py`
6. `tests/`

Compatibility updates required by the existing final validation contract:
- `app/automation/setup_filter.py`
- `app/automation/signal_validator.py`

The bot/webhook layer (`app/bot.py`, `app/main.py`) was not changed by this upgrade.

Behavioral changes:
- 15M is authoritative for intraday entry confirmation; 5M is optional refinement.
- Structural SL uses the deepest relevant invalidation anchor and rejects poor geometry rather than shrinking risk to inflate RR.
- TP1 cannot skip a nearer structural obstacle; TP2 prefers 1H/4H/1D structure.
- Live executable entry is verified and published at the executable quote; SL/TP remain structural and are not blindly shifted.
- Backtest prefilter only locates 15M BOS/retest windows; full engine remains authoritative.
- Backtest includes fees, slippage, 6-hour default expiry, and forensic counters.
- Report includes expectancy, profit factor, drawdown, hold time, direction, regime, and geometry metrics.


> **LEGACY / HISTORICAL DOCUMENT** — This file is retained for history only and is **not authoritative for V11**. The active V11 strategy uses only 1D / 12H / 4H / 1H and the current implementation is defined by `V11_STRATEGY_SPEC.md`.
