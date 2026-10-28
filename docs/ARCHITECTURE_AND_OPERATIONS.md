# Architecture and Operations

## Current operating envelope

This release is a MEXC Futures research and paper-trading application. Live trading is build-locked. Only 1D, 12H, 4H and 1H candles enter strategy decisions. 5M data is used only by the `CHART ... 5M` renderer. The optional AI layer is absent.

Crypto-specific WhatsApp messaging remains blocked by default because the current Meta Business Messaging Policy restricts messaging that facilitates virtual-currency exchange. Do not disguise trade-related content as generic status notifications. `HELP` and `STATUS` are support-only commands and remain available while the gate is closed. Obtain written clearance for the exact intended operation before unlocking crypto content.

## Logical data flow

```text
MEXC public REST
  -> validated MEXC candle / contract / funding records
  -> completed-bar gate and deterministic 12H aggregation
  -> S1-RFPC research hypothesis
  -> independent risk and cost gate
  -> immutable event journal
  -> historical backtest or local paper broker
  -> durable notification outbox
  -> WhatsApp adapter (HELP/STATUS only while crypto policy gate is closed)
```

The current live execution boundary is deliberately absent from the data flow. `app/execution.py` provides an explicit rejecting adapter; the main MEXC client implements public GET requests only.

## Important components

- `app/mexc/client.py`: public Futures REST client, conservative endpoint-family throttling, bounded GET retries, strict official host, paginated kline retrieval, contract/ticker/depth/trade/funding queries. No authenticated order API exists here.
- `app/market/candles.py`: canonical candle type, parser, data checks and 12H aggregation from exactly three aligned completed 4H bars.
- `app/services/market_data.py`: history loading, contract eligibility checks and price-field consistency. If configured `real*` OHLC fields are missing, the request fails instead of mixing different price series.
- `app/strategy/rfpc.py`: deterministic research hypothesis `S1-RFPC-0.2.0-1H-trigger`. Unvalidated and not evidence of an edge.
- `app/risk.py`: independent vetoes and quantity/risk calculations for simulation. Values are provisional.
- `app/backtest.py`: fixed-payout and exchange-realistic modes, 1D/3D/7D/30D/90D windows, chronological 1H event simulation, adverse same-bar resolution and explicit warnings.
- `app/paper.py`: local simulated positions only.
- `app/alerts.py`: persistent price, percent-change and RSI crossing alerts.
- `app/charting.py`: 5M-only visualization module, isolated from the strategy engine.
- `app/commands.py`: strict WhatsApp command parser, no LLM parsing.
- `app/whatsapp.py` and `app/main.py`: HMAC verification on raw body, sender allowlist, durable inbound/outbound records and delivery-status reconciliation; only `HELP` and `STATUS` are processed while the crypto gate is closed.
- `app/services/scheduler.py`: opt-in hourly-close-driven paper worker and alert poller. The alignment gate prevents the paper worker from operating until a probe is reviewed.
- `app/probe_data.py`: read-only alignment/semantics probe. It submits no orders and uses no credentials.

## Environment switches

All these settings are false by default:

```dotenv
SCHEDULER_ENABLED=false
PAPER_TRADING_ENABLED=false
DATA_ALIGNMENT_PROBE_APPROVED=false
AUTO_TRADING_ENABLED=false
LIVE_EXECUTION_ENABLED=false
LIVE_EXECUTION_BUILD_RELEASED=false
WHATSAPP_CRYPTO_MESSAGING_ALLOWED=false
```

The alignment probe file is `reports/alignment_probe.json`. The scheduler additionally requires `price_series_semantics_reviewed: true` after a human has reviewed the actual evidence. Do not edit these values merely to bypass a failed probe. The current included report has status `FAILED_NO_DATA`, so the paper gate is closed.

## State and persistence

Render runs one web instance using SQLite on a persistent disk mounted at `/var/data`. This configuration is not suitable for scaling to multiple concurrent web instances. Before multi-worker or multi-instance deployment, migrate to a shared transactional database and rework the required locking/idempotency boundaries.

Raw/derived data artifacts, events, inbox/outbox items, alert definitions, backtest runs and paper-position records are stored through `app/storage.py`. Delivery accepted by Meta is not reported as delivered until a delivery-status webhook is received.

## Data-alignment probe

Run only in a network environment permitted to reach MEXC:

```bash
python -m app.probe_data --output reports/alignment_probe.json --symbols BTCUSDT,ETHUSDT,SOLUSDT
```

Expected review items include timestamps, gaps, 1H-to-4H/1D aggregation comparison, 12H availability from 4H, contract metadata, price-field coverage and funding fields. The probe's successful network response alone is not enough: the report must show all deterministic checks passed and a human must resolve the semantics of the chosen price series.

## Test commands

```bash
python -m compileall -q app tests
pytest -q
ruff check app tests   # requires Ruff from the optional test dependencies
```

The automated tests use synthetic fixtures and mocked network responses. They do not verify live exchange compatibility, profitability, Meta policy eligibility, the operator's account, or live order safety.
