# Release Notes — v0.8.3 Active S1-RFPC Portfolio Risk & Reporting

## Scope

This patch is based on `PAK_TRADING_ACADEMY_MEXC_S1_RFPC_v0.8.2_FIXED.zip`.
It preserves the active S1-RFPC strategy and its completed-candle timeframes:

`1D → 12H → 4H → 1H`

The legacy 3M RSI/EMA and MTF-TPR modules remain inactive for the active `SCAN` and `BACKTEST` command path. Live order execution remains disabled.

## Changes

- The active research backtester enforces `BACKTEST_MAX_POSITIONS` from 1 through 50.
- Each new exchange-realistic research position is sized under the configured per-position initial-margin cap (default `$1`), risk-per-trade cap, aggregate open-risk cap, total-notional cap and maximum leverage.
- Contract maximum leverage and maintenance-margin metadata are required for a position to be accepted. Missing values reject the simulated entry rather than silently inventing a leverage or MMR.
- Public contract risk-tier metadata is applied when supported by the returned metadata shape. When only base MMR is available, the result records `BASE_MMR_FALLBACK` and the report is marked not validation-ready for full margin-tier verification.
- A shared-wallet equity snapshot estimates aggregate unrealized P&L, maintenance margin, liquidation fees and available margin. It applies a conservative portfolio stress check before ordinary stop/target handling.
- The liquidation price path is explicitly identified as `1H_TRADE_PRICE_OHLC_CONSERVATIVE_STRESS_PROXY`. It is **not** an exchange-exact MEXC Cross Margin liquidation replay because historical Fair Price, external account positions, open orders, order-book impact, partial liquidation and ADL are not replayed.
- Open positions are marked to market at the end of the requested window. Final unrealized P&L includes opening fee, estimated closing fee, estimated closing slippage and accrued funding.
- The report independently reconstructs open-position marked P&L and validates it against the final equity. Missing final marks or inconsistent wallet/position accounting invalidate the report.
- Closed-trade CSV/JSON artifacts now include quantity, contract size, initial margin, leverage, maintenance-margin rate, liquidation-fee rate, tier status and margin mode.
- Added signal direction, position lifecycle, wallet, equity and trade-R reconciliation fields.
- Average signal/fill RR and MAE/MFE include open positions' values-to-date; win rate, expectancy, profit factor and realized R remain based on closed trades.
- Backtest progress records and logs include best-effort process RSS memory telemetry during symbol loading and window completion.
- The active backtester now rejects `BACKTEST_MARGIN_MODE=isolated` during settings validation; this release supports the configured CROSS research model only. The separate legacy paper module remains limited to one open paper position via `MAX_CONCURRENT_POSITIONS=1`.

## Configuration used by the active research backtester

- Starting wallet: `DEFAULT_STARTING_BALANCE=100`
- Per-position initial-margin cap: `INITIAL_MARGIN_USDT=1`
- Maximum leverage: `MAX_LEVERAGE=50`, additionally capped by contract metadata
- Maximum concurrent research positions: `BACKTEST_MAX_POSITIONS=50`
- Aggregate open stop-risk cap: `BACKTEST_MAX_OPEN_RISK_FRACTION=0.25`
- Total notional cap: `BACKTEST_MAX_TOTAL_NOTIONAL_MULTIPLE=25`
- Margin mode: `BACKTEST_MARGIN_MODE=cross`
- Research risk per trade: `RISK_PER_TRADE_FRACTION=0.005`

The 50-position value is a hard ceiling, not a promise to open 50 positions. Available shared collateral, contract minimums, leverage caps, notional limits and aggregate risk can reduce the actual count.

## Known limitations / not claimed as fixed

- This is a shared-wallet Cross Margin **research approximation**, not a complete replica of MEXC account liquidation. Historical Fair Price and exchange partial-liquidation/ADL behaviour are not present in this active S1-RFPC history feed.
- Historical order-book depth and partial fills are not reconstructed. Market fills, taker fees and slippage remain modeled assumptions.
- Funding history can be incomplete; such runs are explicitly marked not validation-ready and must not be treated as fully funding-adjusted.
- The backtest stores the exact current top-turnover universe snapshot used for a run, but this does not remove survivorship bias for historical top-100 membership. It is not a point-in-time historical universe.
- Historical 30D top-100 execution and production memory usage were not live-verified in this patch. Existing bounded data-fetch concurrency remains; RSS telemetry was added, but full streaming/two-pass archive replay is not implemented here.
- The legacy 3M RSI/EMA and MTF-TPR modules are not activated or merged into S1-RFPC.
- The separate paper-trading engine remains capped at one open paper position.
- WhatsApp crypto signal messaging remains disabled behind the existing policy gate. This patch does not bypass Meta policy.
- No live MEXC account, Render deployment, external PostgreSQL service or live historical backtest was executed during local validation.

## Local validation

Run on the patched source:

```bash
python -m compileall -q app tests
pytest -q
```

The current local environment used Python 3.13.5. The Dockerfile targets Python 3.12, so the full test suite should also be run in the actual deployment image before production rollout.
