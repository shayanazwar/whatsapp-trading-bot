# Release Notes — v0.8.5 Runtime Enablement

## Scope

This patch enables the complete supported command catalog for configured WhatsApp senders, activates the default research scanner and signal-delivery features, and preserves the active S1-RFPC strategy on completed `1D → 12H → 4H → 1H` candles. Live order execution remains hard-locked off.

## Changes

- Removed the separate crypto-research WhatsApp clearance gate from active runtime code and deployment configuration. All commands in `HELP_TEXT` now enter the same deterministic command processor for allowlisted senders.
- Removed the HELP/STATUS-only support route and its generic command-blocked response.
- Enabled scanner, signal-push and paper-simulator feature flags by default. The forward paper simulator still requires a genuine, successful data-alignment report before it can open simulated positions.
- Kept webhook HMAC validation, sender allowlisting, phone-number ID validation, message-ID idempotency, API credential checks, outbox accounting and the monthly outbound cap.
- Hard-forced both `live_execution_enabled` and `live_execution_build_released` to `false`, regardless of environment values. No order-placement client or implementation was added.
- Kept the dedicated worker as the backtest consumer; the web service queues durable jobs in shared storage and the worker claims, executes and persists them. Completed reports can now be sent to the originating allowlisted sender.

## Verification

The local regression suite and compile checks are run against the patched source. A live MEXC historical-data run and a live Meta delivery were not performed in this environment.
