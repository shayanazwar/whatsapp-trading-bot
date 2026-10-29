# Architecture and Operations — v0.4.0

## Current scope

This is a backtest-first MEXC USDT-margined Futures scanner and research dashboard for a 3-minute LONG-only RSI/EMA strategy. It derives completed 3-minute candles from contiguous, epoch-aligned 1-minute candles. It records candidate signals; optional paper trading is simulated and off by default. **There is no released private order path in this version.**

## Components

- `app/mexc/client.py`: public MEXC Futures REST requests, bounded retries, throttling, candles, contract metadata and funding history.
- `app/mexc/stream.py`: public all-contract ticker WebSocket stream with bounded reconnect behavior.
- `app/market/candles.py`: validated candle types plus deterministic 3M-from-1M, 12H-from-4H and 3D-from-1D aggregation.
- `app/strategy/rsi_ema.py`: completed-candle RSI, EMA and volume confirmation; LONG only.
- `app/backtest_rsi_ema.py`: historical simulation, quantity sizing, costs, liquidation approximation, trade ledger and reports.
- `app/paper_rsi_ema.py`: optional simulated open/close lifecycle; no HTTP order submission.
- `app/worker.py`: scanner, event persistence and optional paper positions; PostgreSQL advisory lock blocks a second worker.
- `app/main.py`: FastAPI dashboard, protected artifact routes, health endpoint and WhatsApp support webhook.
- `app/storage.py`: SQLite for local work or shared PostgreSQL on Render.

## Strategy rules

A candidate needs RSI(14) at or above 70 and rising, recent EMA21/EMA50 crossover, EMA21 > EMA50 > EMA100, price above EMA21 and the prior candle high, plus relative volume confirmation. The scanner monitors the eligible contract universe and only records a candidate when the strategy conditions pass. No EMA200 crossover is required for entry; EMA200 is a chart overlay only.

## Safe run/deploy behavior

`LIVE_EXECUTION_ENABLED=false`, `LIVE_EXECUTION_BUILD_RELEASED=false`, `AUTO_TRADING_ENABLED=false`, `PAPER_TRADING_ENABLED=false`, and WhatsApp crypto messaging is blocked. Render uses a separate web service and worker with a shared PostgreSQL database. The dashboard uses `WEB_API_TOKEN`. Use `render.yaml` only after reviewing resource plans and prices in Render.

## Known limitations

- Public MEXC live API and WebSocket reachability were not confirmed from the current build environment due DNS resolution failure.
- No verified live-market backtest results are included; synthetic fixtures are unit-test inputs only.
- Authenticated private orders, protective order lifecycle and live position reconciliation are not implemented/released.
- Liquidation is approximated in the simulator; the user's requested $3 stop can be beyond liquidation for $1 margin at high leverage.
- WhatsApp crypto trade signals/charts/balance messages remain blocked due the published business policy; use the protected dashboard for reports and charts.
