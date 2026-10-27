# Report Forensics and Consolidated Decisions

Date: 26 October 2026  
Purpose: implementation traceability, not proof of a trading edge.

## Source inventory

| Source | Main contribution | How it is treated |
|---|---|---|
| `Independent_MEXC_Futures_Research_Architecture_Report.txt` (uploaded) | Detailed candidate strategy, risk and statistical design; MEXC and Meta research claims | Source evidence to audit; its conclusions are not automatically accepted |
| `MEXC_WhatsApp_Futures_Independent_Design_Report.pdf` (uploaded, 42 pages) | Current API/fee discussion, explicit Meta policy risk, staged gates, and design boundaries | Main recent source for policy and execution blockers; factual claims remain subject to source and runtime verification |
| Earlier 23-page independent design reports | Greenfield modular architecture, deterministic 12H, cost-aware backtest and paper broker | Duplicate copies do not count as independent corroboration |
| Earlier 25-page and 47-page reports | Claim audits, component diagrams, strategy hypotheses, cost and validation guidance | Compared against later sources; stale claims are not retained merely because repeated |
| Reports pasted into the chat | Fixed requirements, backtest/payout expectations, operational rules, and current disagreements | Treated as part of the specification, with conflicts recorded below |
| Uploaded 12.15-second screen recording | Last visible work state shows “Editing Symbol Validation And Parsing”; preceding visible status mentions worker schema, 1H timing tests, project configuration/database/commands/tests, and implementation review | Used to resume the code work at strict symbol validation and command parsing; it does not supply market data or trading results |

## Decision matrix

| Topic | Conflicting or weak claims | Decision in this codebase | Status / rationale |
|---|---|---|---|
| API fees | Older launch rates, web/app promotional rates and other summaries conflict | Defaults to the later published API schedule: 0.06% maker / 0.08% taker; account/contract rates remain unverified | Cost assumption, not the operator's confirmed fee tier. See [MEXC fee update](https://www.mexc.com/announcements/article/updates-to-api-futures-trading-fees-jun-1-2026-17827791535742) |
| Kline intervals | A third-party interval list mentions 12H; official Futures kline interval list does not | No native 12H request; derive 12H from three complete, contiguous 4H candles only | Aggregation is unit-tested; empirical exchange alignment failed to be established in this runtime. See [MEXC Futures kline docs](https://www.mexc.com/api-docs/futures/market-endpoints/get-candlestick-data) |
| Kline limits | One supplied note calls the 20 requests / 2 seconds rule unverified | The inspected official endpoint documentation states 20 requests / 2 seconds and up to 2,000 bars per request | Implemented as conservative per-family throttling and bounded pagination; this is not permission to run continuously at the ceiling |
| `open` vs `realOpen` | Some reports interpret `real*` as raw executable prices; the endpoint payload alone does not prove that semantic | The probe records both-field availability and comparisons; configured `real` mode fails closed if any required raw OHLC field is absent | Semantics remain unresolved because public data could not be fetched from this runtime. Do not treat the label itself as proof |
| Primary strategy | Reports propose EMA time-series trend, pullback continuation, breakout/retest, mean reversion and liquidity-sweep variants | Implements one deterministic RFPC-style breakout/retest/reclaim research hypothesis; no strategy is claimed profitable | This is a reproducible starting hypothesis, not an evidence-backed winner. Alternative strategy proposals remain documented, not blended into a mega-strategy |
| Timing | Some plans are vague about whether a setup or trigger can use a still-open candle | Strategy consumes completed 1D/12H/4H/1H candles; the simulation enters at the next 1H open | Unit-tested timing guardrails; historical live-data equivalence is unverified |
| 5M chart | Earlier research documents banned all lower candle intervals; the final implementation prompt explicitly allows a 5M charting exception | `CHART <symbol> 5M` is visualization-only, with EMA 21/50/100/200 and swing-derived zones; it is not an input to strategy, scoring, backtesting or orders | Isolated in `app/charting.py` and strict command parser |
| Backtest P&L | Fixed payout amounts can be misreported as exchange profit | Separate modes: hypothetical fixed payout (TP 5% / SL 2%, +$20 / -$5, independent $100 starting balance) and modeled exchange-realistic P&L | The fixed-payout mode intentionally does not deduct exchange fees a second time; realistic mode depends on data, contract, fee, slippage and funding assumptions |
| Historical OI/depth | Reports disagree on the availability of suitable historical series | No historical OI or book-depth feature is assumed without validated coverage | Current book and recent trades can be queried for execution research only; they do not become hidden lower-timeframe strategy candles |
| Meta/WhatsApp policy | Older reports assume ordinary trade reports are permitted; the recent attachment identifies an explicit virtual-currency restriction | All crypto-related WhatsApp command processing and message dispatch remain blocked by default; never disguise crypto content under a generic label | Written Meta clearance for the exact use is a blocking dependency. A config flag is not proof of permission. See [Meta Business Messaging Policy](https://whatsappbusiness.com/policy/) |
| MEXC sandbox | Some discussions mix web demo trading with API test capability | No API sandbox is assumed. Paper broker is local only and never sends exchange orders | No order was submitted; no exchange fill is claimed |
| Live execution | Master prompt asks for eventual automatic trading; research reports say unknown order/stop semantics and no sandbox | Live executor remains build-locked; no signed private order client or live order path is present in this release | This is a deliberate safety limitation until endpoint semantics, policy, account eligibility, order protection and reconciliation have a separate review |
| GitHub / Render | User asks for deployment through GitHub and Render | Includes Docker, a Render Blueprint, persistent-disk configuration and CI workflow | No remote repository was created/pushed and no Render service was deployed |
| AI provider / platform | Earlier proposals sometimes prescribe a model or coding platform | No model or AI provider is required or included in the decision path | Core application is deterministic and vendor-neutral |

## Why the reports' performance figures are not used

The supplied reports repeat conflicting backtest numbers, including very different outcomes on short and longer historical windows. They do not provide a single reproducible canonical dataset, frozen strategy hash, complete event ledger, fee/funding records and independent reproduction for all claims. The figures are therefore unverified statements, not acceptance evidence. This implementation makes no win-rate or profitability claim.

## Implementation-specific choices

1. **Symbol parsing is strict.** Commands normalize typical forms such as `BTCUSDT` and `BTC_USDT`. Syntax validation does not imply that a contract exists or is currently eligible; those conditions are checked against MEXC metadata for analysis.
2. **One command grammar is used.** Arbitrary natural-language instructions are not interpreted as system commands.
3. **Fixed-payout backtests are separate from exchange-style P&L.** The fixed payout is a hypothetical outcome ledger, not an exchange account simulation.
4. **No external order is submitted.** The `LiveExecutionAdapter` always raises `LiveExecutionDisabled` in the present build.
5. **No fake market history is used.** The public data probe failed with a connection timeout. Its output is retained in `reports/alignment_probe.json` and correctly marks the checks as unresolved.
6. **The paper scheduler is opt-in.** In addition to `PAPER_TRADING_ENABLED=true`, it requires `DATA_ALIGNMENT_PROBE_APPROVED=true`, an alignment report passing the deterministic checks for every requested symbol, and an explicit human review marker for the chosen price-series semantics.
7. **WhatsApp remains policy-gated.** No crypto-specific response is returned or sent unless the configuration gate and written clearance reference are present. The operator must obtain actual Meta clearance; filling in a reference string alone does not establish compliance.

## Current verification record

- `python -m compileall -q app tests`: passed.
- `pytest -q`: 55 passed on the latest run.
- `ruff check app tests`: not run because Ruff is not installed and package installation failed due unavailable package-network access.
- MEXC public-data probe: `FAILED_NO_DATA`, `ConnectTimeout` fetching `BTC_USDT` klines; no empirical conclusion for UTC alignment, `real*` semantics, candle revisions or historical availability.
- Live MEXC orders: none.
- Meta messages: none sent during this work.
- GitHub push / Render deploy: not performed.
