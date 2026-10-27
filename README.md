# MEXC Futures + WhatsApp Research Bot

**Implementation status:** research/backtest/paper components implemented; automated tests are local-only; no account has been connected; no live order has been placed. Live execution is hard build-locked. WhatsApp cryptocurrency/trading content is policy-blocked by default.

## 1. What is included

- Public-data-only MEXC Futures REST adapter (no Binance or other exchange fallback).
- Contract metadata and funding retrieval, request throttling and public payload archive.
- Closed-candle validation and deterministic 12H construction from three contiguous closed 4H candles.
- Strategy hypothesis `S1-RFPC-0.2.0-1H-trigger`: completed 1D regime, derived 12H pullback context, 4H setup and a completed 1H final confirmation. This is an unvalidated research hypothesis, not a profitable strategy claim.
- Multi-timeframe analysis and indicator summaries.
- 5M visualization command isolated to charting; its data never enters strategy decision functions.
- Backtest windows 1D, 3D, 7D, 30D and 90D, each with its own starting balance.
- Two separate P&L modes: fixed payout (+$20 TP / -$5 SL, with 5% TP and 2% SL price distances by default) and a modeled exchange-realistic mode with contract metadata, fees, slippage and funding inputs.
- Persistent local paper positions and an append-only paper event history.
- Persistent price/percentage/RSI alerts.
- FastAPI webhook receiver, raw-body HMAC verification, durable inbox/outbox and sender allowlisting.
- GitHub test workflow, Dockerfile and Render blueprint with durable disk and safe-off settings.
- A public-data alignment probe (must be run in an environment that can reach MEXC).

## 2. Important policy blocker: WhatsApp crypto content

Meta's current WhatsApp Business Messaging Policy lists restrictions concerning buying, selling, promoting or facilitating exchange of virtual currency. This project intends to send crypto market analysis, trading candidates, paper-trade results and related commands. The exact private-owner use case must receive written policy clearance from Meta before that content is enabled.

**Do not disguise crypto-related messages as utility or generic status messages. Do not set `WHATSAPP_CRYPTO_MESSAGING_ALLOWED=true` or fill `WHATSAPP_POLICY_CLEARANCE_REF` unless you have a real written clearance decision that covers this exact use case.** The application blocks WhatsApp crypto content by default. A configuration variable is a gate, not proof that Meta granted permission. If Meta does not permit this use, the WhatsApp feature remains unavailable; this project does not use an unofficial WhatsApp gateway or silently substitute another chat platform.

Policy source: https://whatsappbusiness.com/policy (verify the current version before any deployment).

## 3. Important exchange and research limitations

1. MEXC's official Futures Kline docs do not list a native 12H interval. The app builds 12H from three completed 4H candles, but actual UTC alignment must be empirically verified. The included probe currently cannot reach MEXC from this execution environment; therefore 12H alignment and `open` versus `realOpen` semantics remain unverified here.
2. MEXC's published API Futures fee notice effective 1 June 2026 lists 0.06% maker / 0.08% taker, excluding the noted exceptions. Two taker fills therefore imply ~0.16% of notional in commission alone. The official notice contains inconsistent example prose; contract/account-specific fee fields and the current account schedule must be verified before any cost-sensitive conclusion. The default fee settings are conservative planning parameters, not a claim of your account's rate.
3. MEXC says no Futures API sandbox/test environment is offered. Paper trading in this app is local simulation only; it does not send exchange orders and its fills are not real exchange fills.
4. The strategy is not validated. No backtest was run against a real historical MEXC dataset in this environment. No win rate, expectancy or profitability is claimed.
5. Live execution code is build-locked via `LIVE_EXECUTION_BUILD_RELEASED = False`. It has not passed stop-protection verification, continuous reconciliation, watchdog and recovery review, and it must not be used as live trading software.
6. Render requires durable storage for the SQLite database and generated report files. The provided blueprint mounts a persistent disk at `/var/data` and forces one worker; do not scale to multiple web instances with one SQLite file.
7. The attached report identifies potentially applicable Pakistani virtual-asset regulation. The application does not provide legal advice. Obtain qualified legal review for the intended use and verify MEXC account eligibility in the operator's jurisdiction.

## 4. Local setup

Python 3.11 or newer is required; Python 3.12 is the target Render runtime. `requirements-runtime.lock` pins the Linux runtime dependency set used by the Docker deployment; local development uses the project test extra.

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[test]"
cp .env.example .env
```

Edit `.env` only for non-secret local settings. Never commit `.env`. Public MEXC market-data calls do not require trading credentials. Keep live credentials absent in research/paper phases.

Safe baseline settings are:

```dotenv
SCHEDULER_ENABLED=false
PAPER_TRADING_ENABLED=false
AUTO_TRADING_ENABLED=false
LIVE_EXECUTION_ENABLED=false
WHATSAPP_CRYPTO_MESSAGING_ALLOWED=false
```

Run the service locally:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Check `http://127.0.0.1:8000/healthz`. The root and health endpoints are operational checks, not a user dashboard.

## 5. Empirical data probe

The probe uses public MEXC data only. It checks candle timestamp boundaries, native 4H/1D versus aggregation from completed 1H candles, constructed 12H grouping, gaps, contract metadata, ticker fields, funding fields and the observed presence of `real*` fields. Field differences alone do not establish the semantic meaning of `realOpen`.

```bash
python -m app.probe_data --output data/alignment_probe.json
```

This probe must succeed from a network environment permitted to access MEXC before the 12H aggregation or price-field assumptions are treated as verified. The included run from the current environment failed before exchange server time could be read (`ConnectError` (connection could not be established in this runtime)); see `IMPLEMENTATION_STATUS.md` and the linked probe output. That failure is not a MEXC outage diagnosis.

## 6. Backtesting and P&L modes

Supported command syntax (intended for the WhatsApp command processor after the Meta policy gate is cleared):

```text
BACKTEST
BACKTEST 1D
BACKTEST 3D
BACKTEST 7D
BACKTEST 30D
BACKTEST 90D
BACKTEST BTCUSDT 7D
BACKTEST REALISTIC BTCUSDT 30D
```

- `BACKTEST` means each of the five windows for each configured default symbol. Each run is independent and starts at `$100`.
- `fixed_payout` mode (the default) uses configured `TP_PCT=5`, `SL_PCT=2`, `FIXED_WIN_PAYOUT=20`, `FIXED_LOSS_PAYOUT=5`. The price distances determine whether TP or SL is touched; the payout is the entire modeled result for that outcome. It does **not** deduct fees a second time. This is a hypothetical fixed-payout scenario, not exchange-realistic profitability.
- `exchange_realistic` mode uses MEXC contract-size/volume rules, a risk-sized position, fees, assumed spread/slippage and available historical funding records. It remains a model of execution, not observed exchange fills. If funding or price semantics are unverified, warnings are emitted; this application does not declare the backtest validation-ready.
- Same-candle TP/SL ambiguity is resolved adversely; gaps may fill worse than the stop; incomplete data is rejected rather than filled in.
- Win rate is `n/a` if no trades completed. Open positions at the end are reported separately and are not counted as completed trades.

Current limitation: this runtime could not access the public MEXC API, so real historical periods cannot be run here. Tests use deterministic synthetic fixtures/mocks only and are not performance evidence.

## 7. WhatsApp commands

The command parser and processing path include:

```text
HELP
STATUS
PRICE BTCUSDT
CHART BTCUSDT 5M
ANALYZE BTCUSDT
BACKTEST
BACKTEST BTCUSDT 7D
BACKTEST REALISTIC BTCUSDT 30D
ALERT BTCUSDT ABOVE 70000
ALERT BTCUSDT BELOW 65000
ALERT BTCUSDT CHANGE 3% 1H
ALERT BTCUSDT RSI ABOVE 70 4H
ALERTS
ALERT DELETE ABC123
ALERT PAUSE ABC123
ALERT RESUME ABC123
AUTOTRADE STATUS
AUTOTRADE OFF
SIGNALS
OPEN_POSITIONS
PERFORMANCE
RISK
```

These commands are intentionally blocked from WhatsApp until written Meta policy clearance for this crypto use is obtained and recorded. Their presence in code does not imply Meta permission or that the Cloud API has been configured. Charting uses only MEXC `Min5` data and is isolated from the 1D/12H/4H/1H strategy engine.

To configure Cloud API later, populate `META_VERIFY_TOKEN`, `META_APP_SECRET`, `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID` and `WHATSAPP_ALLOWED_SENDERS` in the deployment environment after completing Meta's current requirements and the policy clearance gate. Do not send secrets over WhatsApp or add them to GitHub.

## 8. GitHub setup

1. Create a private GitHub repository.
2. Review this entire project and `docs/REPORT_FORENSICS.md` before uploading.
3. Ensure `.env`, SQLite databases and `data/` are ignored; commit only `.env.example` with placeholders.
4. Push the project to the repository.
5. The included `.github/workflows/test.yml` installs the test extra, compiles the application and runs `pytest` on pushes and pull requests.

Do not place MEXC or Meta secrets in GitHub Actions secrets unless an explicitly approved workflow actually needs them. Current tests need no live credentials.

## 9. Render deployment

The supplied `render.yaml` defines a single Python web service, `/healthz` health check, one persistent disk mounted at `/var/data`, and safe-off environment variables. It deliberately disables the scheduler, paper loop, auto-trading, live execution and WhatsApp crypto messaging by default.

1. Push the project to GitHub.
2. In Render, create a Blueprint from the repository and review `render.yaml` before applying it.
3. Confirm the service plan supports the defined persistent disk and that disk/service costs are acceptable.
4. Deploy and verify `/healthz`.
5. Configure only the needed non-secret environment values through Render's environment UI.
6. Do not enable WhatsApp crypto-related messages until Meta has provided written clearance. Do not enable scheduler or paper trading until the public-data probe, persistence and operational acceptance checks have passed.
7. Live execution is not enabled by this blueprint and remains build-locked in code.

Use one running web instance with this SQLite/persistent-disk arrangement. If a multi-worker or multi-instance service is later required, migrate the persistence layer to a suitable shared database and redesign idempotency/locking first.

## 10. Running tests and quality checks

```bash
python -m compileall -q app tests
pytest -q
```

Optional linting after installing the test extras:

```bash
ruff check app tests
```

The GitHub workflow runs compilation and tests. Tests mock external calls and do not submit live orders. Passing tests establishes code-path properties covered by the tests; it does not prove strategy profitability, Meta policy clearance, exchange account eligibility, or live execution safety.

## 11. Safety settings and readiness

- `AUTO_TRADING_ENABLED=false`
- `LIVE_EXECUTION_ENABLED=false`
- `PAPER_TRADING_ENABLED=false` initially
- `SCHEDULER_ENABLED=false` initially
- `WHATSAPP_CRYPTO_MESSAGING_ALLOWED=false` until written clearance

The execution class raises `LiveExecutionDisabled` because the code release lock is false. Do not remove that lock without a separate review of stop/TP verification, order state machine, reconciliation, watchdog, risk controls, emergency procedures, API eligibility, relevant jurisdictional requirements and a documented human approval.

## 12. Research references

- MEXC Futures Kline data: https://www.mexc.com/api-docs/futures/market-endpoints/get-candlestick-data
- MEXC contract information: https://www.mexc.com/api-docs/futures/market-endpoints/get-contract-info
- MEXC integration guide: https://www.mexc.com/api-docs/futures/integration-guide
- MEXC Futures order placement: https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order
- MEXC Futures API fee update: https://www.mexc.com/announcements/article/updates-to-api-futures-trading-fees-jun-1-2026-17827791535742
- MEXC API FAQ / no sandbox statement: https://www.mexc.com/mexc-api
- Meta WhatsApp Business Messaging Policy: https://whatsappbusiness.com/policy
- Meta WhatsApp pricing: https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing
- PVARA regulatory information: https://www.pvara.gov.pk/regulations

All current rules and rates should be checked again before deployment because exchange/API and messaging policies can change.
