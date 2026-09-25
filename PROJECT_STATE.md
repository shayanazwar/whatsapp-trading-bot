# Project State — 2026-09-25

## Current release

This package is the corrected deterministic MEXC Futures scanner release for the Pak Trading Academy WhatsApp bot.

## Canonical runtime

```text
Docker -> uvicorn app.main:app
```

The `app/` package is canonical.

## Deterministic decision flow

```text
MEXC Futures data
  -> data integrity / closed candles
  -> 120-symbol universe
  -> BTC market filter
  -> 1D context
  -> 4H regime
  -> 1H direction + protected structure
  -> 15M BOS + post-BOS retest
  -> 5M trigger
  -> momentum / volume / volatility
  -> structural target path
  -> structural SL / RR
  -> hard technical gates
  -> MEXC executable quote / spread / index-fair / funding / depth / trade flow
  -> grouped 100-point score
  -> final validator (score >=82, RR >=2, families >=5/6)
  -> symbol/side cooldown
  -> WhatsApp signal
```

## Safety state

```text
SCANNER_ENABLED=false
AUTO_SIGNAL_ENABLED=false
AUTO_TRADE_ENABLED=false
ALLOW_LIVE_EXECUTION=false
```

These are the package defaults.

## Important limitations

The scanner is deterministic, but a target win rate such as 70–80% is not guaranteed. The score is an evidence score, not a probability.

Live execution is deliberately disabled. Missing live-trading components still include post-fill reconciliation, protective-order verification, partial TP/break-even management, portfolio exposure controls and emergency recovery.

## Verification

```text
pytest -q
17 passed
python -m compileall -q app tests
OK
```
