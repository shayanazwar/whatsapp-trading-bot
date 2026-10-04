# Pak Trading Academy WhatsApp Trading Bot

Production architecture:

```text
MEXC Futures Data
      ↓
Universe / Data Validation
      ↓
1D Macro Regime
      ↓
12H Intermediate Bias (causal aggregation of completed 4H candles)
      ↓
4H BOS / Retest Structure
      ↓
1H Setup / Entry Confirmation
      ↓
Structural SL / TP / Post-cost RR
      ↓
Final Validation + Duplicate / Cooldown Protection
      ↓
WhatsApp Cloud API
```

Only these analysis timeframes are allowed: **1D, 12H, 4H, 1H**. MEXC Futures has no native 12H interval in this project; 12H candles are built from three contiguous completed 4H candles.

Only WhatsApp is supported for messaging. The deployment entrypoint is `uvicorn app.main:app`.

Backtests support `1D` and `7D` periods and always emit a report, including zero-signal runs with gate diagnostics.

Live trading remains disabled unless both live-execution safety flags are explicitly enabled in the environment.
