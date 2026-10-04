# Project State — Pak Trading Academy WhatsApp Trading Bot

## Current architecture

```text
MEXC Futures Data
      ↓
Universe Filter
      ↓
1D  → Macro Regime
      ↓
12H → Intermediate Bias (causally aggregated from completed 4H candles)
      ↓
4H  → Primary Structure / BOS / Retest / S&R
      ↓
1H  → Setup / Confirmation / Entry
      ↓
Risk → SL / TP / RR
      ↓
Final Validator
      ↓
Duplicate / Cooldown
      ↓
WhatsApp Dispatch
```

## Supported analysis timeframes

Only `1D`, `12H`, `4H`, and `1H` are valid analysis timeframes. The 12H series is built locally from exactly three contiguous completed 4H candles so no unsupported exchange interval is required.

## Messaging

WhatsApp is the only supported messaging interface. The inbound path is:

`Meta Cloud API → FastAPI webhook → signature check → message extraction → command router → handler → WhatsApp send API`.

Webhook retries remain retryable when processing fails, while successfully processed message IDs are persisted for idempotency.

## Backtesting

The paper backtester supports `1D` and `7D` runs and uses only completed 1D/12H/4H/1H data. Historical decisions are evaluated at completed 1H closes, and future trade resolution is performed only on later 1H candles. Every run returns a report, including zero-signal runs.

## Risk model

Trade geometry is structural and higher-timeframe oriented. Stops are placed beyond invalidating structure with an ATR-based buffer, targets are anchored to higher-timeframe path/liquidity, and minimum post-cost RR is enforced.

## Operational diagnostics

Scanner logs include aggregate counts for universe, data validation, each approved timeframe, structure, setup, momentum, volume, risk, RR, final signals, duplicates, and errors. WhatsApp dispatch logs include outbound acceptance IDs when Meta accepts a message.

## Deployment

The production container starts `uvicorn app.main:app`. Runtime secrets remain environment variables and are never stored in replacement artifacts.
