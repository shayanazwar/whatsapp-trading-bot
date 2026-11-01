# Patch notes — PAK Trading Academy v0.8.0

## Findings from the supplied screenshot, logs and repository

- The screenshot shows a durable backtest acknowledgement, followed by a stale-message rejection. The earlier 300-second command-age check made a valid delayed/retried webhook command fail. This patch removes that age-based rejection and relies on the persisted Meta message ID for idempotency.
- The old `BACKTEST` route was running the legacy 3-minute RSI/EMA backtester. The repository also contained a separate S1-RFPC swing backtester. `BACKTEST` now routes to S1-RFPC with completed 1D, 12H, 4H and 1H candles, matching the active research scanner.
- The prior backtest simulator limited concurrent positions to one. The active research simulator now supports a configurable ceiling of 50 concurrent positions, one position per symbol, with separate per-trade risk, aggregate stop-risk and gross-notional limits. The independent paper-trading limit remains one, and live order execution remains disabled.
- The previous worker acknowledgement did not expose symbol-by-symbol progress. The worker now records job stages and emits per-symbol load/scan progress, failure detail, completion and exception logs. It also avoids running the scheduled top-100 fetch concurrently with an active backtest job.
- Scheduled scanning previously used the legacy intraday strategy path. It now uses S1-RFPC 1D → 12H → 4H → 1H, with signal delivery checks and signal expiry.
- The worker now queues the completed backtest report to the WhatsApp outbox when the separate crypto-message policy gate is open. Signal push also requires the explicit gate, a configured allowed-recipient list, valid Meta credentials and `SIGNAL_PUSH_ENABLED=true`.
- Backtest reports now expose total/long/short TP and SL counts, open positions, maximum observed concurrent positions, risk limits, and explicit model limitations. Liquidations are marked not modelled rather than falsely reported as zero.

## Important deployment controls

`SIGNAL_PUSH_ENABLED=true` is set in the Render Blueprint, but `WHATSAPP_CRYPTO_MESSAGING_ALLOWED=false` is intentionally retained. Do not enable crypto-related WhatsApp messages unless the operator has confirmed the exact permitted use and has the required written clearance reference. A nonempty `WHATSAPP_POLICY_CLEARANCE_REF` is required along with the flag, allowed sender list and Meta Cloud API credentials.

`BACKTEST_MAX_POSITIONS=50` controls research simulations only. `MAX_CONCURRENT_POSITIONS=1` continues to control the separate paper simulator. `LIVE_EXECUTION_ENABLED=false`, `LIVE_EXECUTION_BUILD_RELEASED=false`, `AUTO_TRADING_ENABLED=false` and `PAPER_TRADING_ENABLED=false` remain unchanged.

## What the 50-position setting does not mean

Fifty is a ceiling, not a guaranteed number of open trades. The simulator also enforces 0.5% modeled risk per new entry by default, aggregate planned stop-risk of up to 25% of current balance, a 3.0× gross-notional cap, contract minimum/step constraints, one position per symbol and strategy eligibility. The simulator does not reproduce all exchange cross-margin maintenance margin, liquidation, historical order-book impact, queue priority or partial fills.

## Verification performed

- Python compile check: passed.
- Test suite: 132 passed.
- Added regression coverage for delayed webhook command idempotency and for opening concurrent simulated positions across symbols.
- No live MEXC API run, Render deployment, PostgreSQL integration deployment or WhatsApp delivery was performed in this environment. The first truncated traceback supplied by the user does not include the final exception type/message, so its exact original exception cannot be identified from those lines alone. The patched worker now emits a full exception traceback and durable job error on failure.

## Files changed from the supplied archive

- `.env.example`, `README.md`, `PATCH_NOTES.md`, `pyproject.toml`, `render.yaml`
- `app/backtest.py`, `app/commands.py`, `app/config.py`, `app/main.py`
- `app/services/command_processor.py`, `app/services/scheduler.py`, `app/storage.py`
- `app/version.py`, `app/worker.py`
- `tests/test_backtest.py`, `tests/test_main_webhook.py`, `tests/test_symbols_and_commands.py`
