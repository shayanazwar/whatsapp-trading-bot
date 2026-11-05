# MEXC Futures + WhatsApp System
## Independent Strategy, Architecture and Implementation Plan

**Version:** 1.0 — planning only  
**Date:** 26 October 2026  
**Scope:** MEXC Futures only; strategy candles limited to 1D -> 12H -> 4H -> 1H; WhatsApp interface  
**Execution:** Live trading disabled; no code implementation started  
**Platform choices:** Coding language, IDE, host, database, AI vendor/model and development assistant remain undecided

---

## 1. Executive decision

**Build a research and paper-trading system first, not an autonomous trader.** The immediate product should be a reliable market-data pipeline, an auditable multi-timeframe strategy experiment, a cost-aware event-driven backtester, a local paper broker, a persistent decision journal and a secure WhatsApp reporting interface. None of the supplied reports establishes a trading edge. The system must be designed to show that an idea is wrong just as clearly as it can show a promising result.

Do not start with AI, WhatsApp commands that change state, live orders, a large indicator stack, a 100-point signal score, or a Monte Carlo dashboard. Those are either optional or downstream of the core evidence. The next activity is a non-destructive public-data alignment and candle-semantics probe. It requires no trading key and determines whether historical data can safely support all later work.

### Non-negotiable requirements

1. MEXC Futures is the sole exchange and market-data/execution venue. Do not use another exchange as a fallback, benchmark feed, arbitrage venue or hidden dependency.
2. Strategy decisions may use only completed 1D, 12H, 4H and 1H candles. No 5M, 15M, 30M or other candle interval may enter strategy logic. Live spread/depth/trade/funding data may be used for execution feasibility when officially available, but not as a disguised lower-timeframe strategy.
3. There is no assumed MEXC API sandbox. Use local paper simulation, not purported exchange test fills.
4. Deterministic code owns timestamps, market data, features, candidate detection, sizing, risk vetoes, position state, command authorization and order state. An optional AI layer can explain already-recorded results only and must be removable without breaking the system.
5. Live execution defaults off. It remains outside the early implementation phases and requires separate evidence, safety tests and explicit human approval.
6. All proposed numeric thresholds are hypotheses or provisional safety settings until tested. Do not describe them as optimized or proven.

---

## 2. What the attached reports say, and where they conflict

The governing 11-page master prompt is the specification. The 15-page forensic implementation plan is preliminary context. The two 23-page reports are byte-identical duplicates, so they add no independent evidence. The 25-page detailed blueprint is the most technically detailed companion. The later 47-page version is useful for its step-by-step design but has material errors and overly precise placeholders that must not be adopted uncritically.

### Material corrections

**API fees.** The 47-page report treats the March launch schedule of 0.01% maker / 0.05% taker as current. That schedule was superseded. MEXC's later official announcement published May 28, effective June 1, 2026, changes API Futures rates to **0.06% maker / 0.08% taker** for applicable pairs and says API rates take precedence over web/app promotions. Use 0.06%/0.08% as the latest published planning baseline, excluding Innovation Zone pairs, and check account/contract-specific terms before any live pilot. Source: https://www.mexc.com/announcements/article/updates-to-api-futures-trading-fees-jun-1-2026-17827791535742

**Fee arithmetic.** Two taker legs at 0.08% imply approximately 0.16% of traded notional in round-trip fees before spread, slippage or funding. If stop distance is 1% of entry and the position is sized so that the gross stop loss is 1% of notional, this approximates 0.16R in fees. The often-mentioned 0.02R comparison applies only to two maker legs each charged at 0.01%; it is not the round-trip cost under a 0.05% taker rate. At 0.05% taker each way, the fee-only round trip is 0.10% notional, or approximately 0.10R at a 1% stop. A maker entry plus taker exit at 0.06%/0.08% costs about 0.14% notional. Maker fills are not guaranteed; the simulator must model fill type rather than assume it.

**Kline rate limit.** One user-supplied note describes the limit of 20 requests per 2 seconds as unverified. The current official MEXC candlestick endpoint page explicitly lists this limit and a maximum of 2,000 data points per request. The official page lists Min1, Min5, Min15, Min30, Min60, Hour4, Hour8, Day1, Week1 and Month1, with no native Hour12. Source: https://www.mexc.com/api-docs/futures/market-endpoints/get-candlestick-data

**Contract endpoint path.** Some reports refer to `/api/v1/contract/detail`; the currently displayed contract-info page documents `GET /api/v1/contract/detail/country`, with a 10 requests / 2 seconds limit and fields including `contractSize`, `priceUnit`, `volUnit`, `minVol`, `maxVol`, margin/risk tiers and `apiAllowed`. Use the currently documented endpoint after a contract test, and isolate path differences inside the adapter rather than scattering them through the app. Source: https://www.mexc.com/api-docs/futures/market-endpoints/get-contract-info

**Candle-price semantics remain unresolved.** Reports infer that `open` may be continuity-adjusted and that `realOpen` is the actual first trade. The current REST documentation page does not fully explain this relationship, while examples/stream payloads may expose additional fields. Do not treat the interpretation as proven. Store raw payloads, compare REST and WebSocket fields, inspect fair/index-price series, and resolve stop trigger semantics before accepting backtest fills as realistic.

**Tuning windows are burned.** The preliminary plan repeats a 7-day result of +43.3R / PF 2.51 and a 30-day result of -31.6R / PF 0.73 for a prototype. Those outcomes were not independently reproduced here; their event ledgers and exact config hashes were not supplied. Treat them only as reported historical claims. Every inspected/tuned window is development data, not a clean final holdout. A label/runtime discrepancy such as “2.50 ATR” while code allegedly ran a different number makes affected tuning results even less trustworthy.

**Strategy constants conflict.** One version proposes a 2.5 x ATR impulse and a narrower pullback region; another formalizes 1.5 x ATR and a wider zone, a 1.5R target and a 48-hour time stop. None of those constants is empirically established. The plan below defines one reproducible starting hypothesis and a tiny pre-registered sensitivity set. Exact parameters are frozen before a locked holdout is touched.

**Order identity is not a guarantee of exactly-once execution.** The Place Order page documents `externalOid`, but the existence of a client identifier does not by itself prove server-side idempotency semantics. Persist intents before sending; treat a timeout as an unknown state; query orders and positions before retrying. Never issue a blind duplicate.

**WhatsApp cost figures are estimates until the live rate card is checked.** Contemporary secondary pricing pages report approximately USD 0.015 per delivered Pakistan utility/service message effective October 1, 2026 and a first-1,000 monthly service-message allowance. Meta's live rate card and any business solution provider markup/taxes must be checked before budgeting. Do not make high-frequency notifications the product.

---

## 3. Product definition: what this system should be

The product is an **auditable trading-research and operating system** with four capabilities that share the same validated input data:

- **Scan:** monitor the eligible MEXC Futures universe and generate explainable candidates from completed 1D/12H/4H/1H candles.
- **Evaluate:** calculate trade geometry, contract-aware size, fee/funding/slippage estimates, exposure impact and rejection reasons.
- **Test:** replay the same deterministic strategy through historical tests and forward paper trading using an event ledger, not a separate optimistic simulator.
- **Report:** send low-volume, clearly labelled WhatsApp summaries and accept secure read-only status commands plus PAUSE.

A live executor is a later, separate safety boundary, not a feature of the first release. A language model is not required for any of the four essential capabilities.

### Definitions for every report

- **Analysis:** market description only; not a candidate or order.
- **Candidate / proposed signal:** a rule-matching plan, not an order and not an executed trade.
- **Paper fill / paper trade:** a simulated event under explicitly stated assumptions.
- **Order submitted:** only after a real API request is recorded and its response/state is reconciled.
- **Confirmed fill:** backed by exchange order/deal records.
- **Realized result:** reconciled closed-position P&L net of exchange fees, funding and modeled/observed execution costs.

Never mix these labels or show paper profits as account profits.

---

## 4. Recommended architecture

Use a **modular monolith** for the initial research and paper stages. It is easier to test end-to-end determinism and reduces distributed-system failure modes. Split processes only when there is a concrete isolation, durability or concurrency requirement. Keep the future executor isolated from the scanner and notifier.

```text
MEXC Futures public REST / WebSocket
  |-- klines, contract info, funding history
  |-- optional current trades/depth for live feasibility only
  v
[Exchange adapter: schemas, rate budget, retries, clock, raw payloads]
  v
[Immutable raw-data snapshots + canonical candle store]
  v
[Universe snapshots + data freshness / completeness validator]
  v
[Deterministic 12H builder: three contiguous completed 4H candles]
  v
[Causal feature/structure engine: 1D -> 12H -> 4H -> 1H]
  v
[Baseline setup detector -> candidate evidence]
  v
[Trade planner -> fees/funding/spread/slippage estimator]
  v
[Independent hard-risk vetoes -> allow / reject + reasons]
  |---> [Append-only decision journal + config/data hashes]
  |---> [Event-driven backtest engine]
  |---> [Paper broker + position/fee/funding ledger]
                 |                    |
                 v                    v
         [WhatsApp report/API]   [Metrics + drift monitor]
                 |
        [Signature-verified webhook + deterministic command/RBAC]
                 |
        [Optional read-only explainer, much later]

[Future MEXC private executor]
  Separate process/key; not in early release; permanently OFF by default.
  Must reconcile UNKNOWN order state before any retry. Never controlled by AI/chat.
```

### Logical components and contracts

1. **MEXC adapter:** only component that knows endpoint paths, response schemas, request limits and signing details. Return normalized values plus raw response/version metadata. Inspect both HTTP status and MEXC `success`/`code` fields; HTTP 200 is not enough.
2. **Universe manager:** snapshot eligible contracts and contract metadata at each decision date. Filter for enabled state and `apiAllowed=true`, valid contract type, sufficient history and data quality. Preserve snapshots to reduce survivorship bias; explicitly disclose any history for delisted symbols that cannot be obtained.
3. **Candle store and validator:** deduplicate by symbol/timeframe/open time, validate monotonic time and OHLC invariants, check freshness/completeness, preserve raw candles and corrections, and version data snapshots. Missing data means NO_TRADE, not interpolation or a fake fallback.
4. **12H builder:** build from three unique, contiguous, completed 4H bars only after observed UTC alignment has been verified. Aggregate open=first open, high=max(high), low=min(low), close=last close, volume/amount=sum. If any child is missing, duplicated, overlapping or incomplete, reject the 12H bar. Save the child timestamps and aggregation version.
5. **Feature/structure engine:** strictly causal calculations; every feature records the timestamp when it became available. Confirmed pivots must not be backdated as if they were known at the pivot candle. No future candle may change a past decision.
6. **Setup detector:** one strategy hypothesis at a time, emits raw evidence and deterministic signal IDs. Do not let ranking thresholds silently become a strategy rule.
7. **Trade planner / cost model:** plan entry, stop/invalidation, target and exit policy; calculate exact value using MEXC contract size/multiplier and precision; estimate fees, funding, spread, slippage, partial fill and gap effects. Make fee assumptions explicit and versioned.
8. **Independent risk engine:** consumes the plan and state, but does not depend on an optional score or AI output. Every veto has a code and reason. Vetoes cannot be overridden by a score.
9. **Backtester and paper broker:** consume the same strategy and cost functions. Backtest is an event-driven replay; paper is a forward event stream. Same config and same frozen input must yield the same result.
10. **Decision/trade ledger:** append-only events for input timestamps, data/config hashes, candidate details, every passed/failed gate, plan, intended order, assumed/reconciled fill, fees/funding/P&L, position state, balance/equity and system errors. Corrections create new events, not silent rewrites.
11. **WhatsApp gateway:** receives webhooks, verifies signature on raw bytes, authorizes sender and parses commands deterministically, deduplicates message IDs and sends status/report templates. It cannot mutate core risk configuration or enable live execution.
12. **Monitoring/recovery:** independent heartbeat/alert route beyond WhatsApp, freshness metrics, scheduler locks, retry budgets, gap backfill, restart reconciliation and kill switch.
13. **Future executor:** separate process and minimal-permission key; only considered after prior gates; does not share code paths that allow a natural-language message to produce an order.

### Storage / deployment principles (no vendor choice yet)

- Persist raw market payloads and canonical OHLC data separately from derived features and trades. Use Parquet or equivalent immutable snapshots for research histories; use a transactional DB for decisions/order events.
- A single-writer local database can be acceptable for a research prototype only on durable storage and with tested backup/restore. Move to a multi-user transactional database when concurrent services or ledger integrity demand it.
- The scheduler must use a single-flight lock or job lease. Missed or overlapping scans should be skipped/logged, not duplicated.
- A static egress IP matters only if/when MEXC key restrictions require it. There is no need to pay for live-execution infrastructure in research phases.
- Choose language, framework, host and AI provider later with a scored requirements matrix. No architecture decision should force an AI dependency.

---

## 5. Baseline trading hypothesis (research candidate, not a trading recommendation)

### H1: Multi-timeframe trend continuation after pullback and 1H sweep/reclaim

**Claim to test:** in directional regimes where 1D and 12H structure agree, a 4H impulse and pullback followed by a completed 1H sweep-and-reclaim produces positive net expectancy compared with a simple direction-aligned baseline, after realistic costs. It may fail; the test should be capable of rejecting it.

#### Timeframe responsibilities

- **1D — regime permission:** classify directional structure as bullish, bearish, neutral or conflicted. A minimal causal swing definition is preferable to a pile of indicators. For a structure pivot using two bars on each side, it only becomes known after the two right-side daily candles have closed.
- **12H — bias confirmation:** same kind of causal structure reading. It must agree with 1D; if either is neutral, insufficiently formed or conflicting, no H1 setup is active. Because 12H is derived from 4H, it is unavailable until all three child bars have completed and passed validation.
- **4H — setup:** identify a confirmed break of a prior swing and a directional impulse. Starting candidate: net displacement at least 1.5 x ATR(14, 4H) over a predeclared three-bar window. The pullback must enter a predeclared value region, initially 38.2%–78.6% of that measured leg, without invalidating the origin swing. These values are placeholders.
- **1H — trigger:** for a long, a completed 1H candle trades below a previously confirmed 1H swing low and closes back above that swing level; for a short, it sweeps a previously confirmed swing high and closes back below it. The swing must have been known before the trigger candle.
- **Entry:** next 1H candle open, modeled as taker with conservative adverse slippage. The trigger close is not the assumed fill. A limit-entry variant is a separate registered experiment and must account for non-fills and adverse selection.
- **Stop:** outside the trigger candle's sweep extreme by a predeclared ATR buffer (candidate 0.15 x ATR(14, 1H)), rounded outward to the legal price tick. Reject nonsensical geometry and sizes below minimum quantity; do not move stops solely to force a desired trade count.
- **Target and time exit:** initial experiment uses one fixed gross 1.5R target and no partial exits/trailing stops. Candidate max holding window: 48 completed 1H bars, then exit at a conservative next executable price. Test a 2.0R target or a narrower pullback zone only as a named alternative on development/validation data, not by repeatedly inspecting the final holdout.
- **Shorts:** implement symmetrically. Report long and short results separately; do not assume they perform equally.

#### Why this is the baseline

It uses each permitted timeframe for one distinct purpose, provides an objectively timestamped trigger and invalidation, and can be falsified with candle data and historical funding. It is less complex than combining trend, range breakout, mean reversion, funding, OI, depth and numerous overlapping indicators in one rule. It is not presumed profitable.

#### What should not be mandatory at first

Do not require every indicator family—EMA alignment, RSI, MACD, stochastic, volume surge, funding extreme, OI change, depth imbalance, VWAP distance and a high composite score—simultaneously. This duplicates evidence, increases false certainty and can starve the experiment of trades. Begin with the structural hypothesis plus hard safety gates. Record additional information and test incremental additions by ablation only after the baseline is evaluated.

#### Limited alternatives

- **H2, later:** 1D/4H range breakout-retest with 1H confirmation, only as a separate pre-registered experiment.
- **H3, later:** 1D/4H range-bound extreme mean reversion with a funding-crowding filter, only if data coverage and range-state rules are definable.

Do not code H1/H2/H3 as a blended mega-strategy. Finish data correctness and the H1 research harness first. More variants increase selection bias; every attempted parameter/hypothesis must be logged.

---

## 6. Candidate filtering, ranking and hard safety vetoes

Separate **strategy conditions** from **safety vetoes**. H1 structure rules determine whether a candidate exists. Safety vetoes determine whether a candidate may be considered tradable. The quality score, if introduced later, may only rank candidates that already passed both layers.

### Non-negotiable vetoes

- Required candle missing, duplicated, incomplete, stale, misaligned or derived from an invalid 12H child set.
- Any higher-timeframe information was not complete and available at the decision time.
- Contract inactive, `apiAllowed` false, unsupported product type, broken symbol history or invalid contract metadata.
- Contract precision, tick, volume step, min/max quantity or contract multiplier cannot be validated.
- Estimated fee + funding + spread + slippage overwhelms the registered risk/reward geometry.
- Available data cannot justify an execution claim; a backtest must not treat current depth/OI as historical features.
- Position/order/account state unknown or unreconciled (only applies to any future live path); duplicate signal/position not resolved.
- Position size would exceed the permitted cash risk, portfolio risk, correlation bucket, margin or liquidation buffer.
- The global pause/kill switch is active, clock drift invalidates signed requests, or data/API circuit breaker is open.

Use exclusive first-fail reporting so every rejection has a primary reason, plus an optional list of additional failed checks. Maintain shadow evaluation of rejected candidates only within development data to learn whether a gate has predictive value. Do not remove genuine safety gates simply to increase trade count. Do not set fixed volume/spread cutoffs such as USD 5M turnover or 0.12% spread as universal truths; calibrate liquidity requirements to intended notional and conservative fill-cost estimates.

### Optional quality ranking, only after evidence

First release need not have a 0–100 score. If candidate ranking becomes necessary due to simultaneous valid setups, use a small number of distinct evidence families: higher-timeframe alignment strength, 4H impulse/pullback quality, 1H reclaim characteristics, volatility state and cost efficiency. Scores are ranks, not probabilities. Test calibration in bins and on out-of-sample results; scores never override a veto.

---

## 7. Contract-aware risk, sizing and cost accounting

### Required sizing inputs

Account equity (or explicit simulated equity), intended risk budget, entry, stop, contract size/multiplier, quote/settlement currency, `priceUnit`, `volUnit`, `minVol`, `maxVol`, risk tiers/maintenance margin, fees, estimated slippage, spread and funding-holding assumptions. Do not simplify contract sizing to `quantity * price` unless that matches the actual MEXC contract's documented unit semantics. The sizing implementation must have worked examples and cross-checks for each supported contract type.

### Consistent risk accounting

1. Calculate stop-out loss per legal contract using the exact contract multiplier and the adverse stop fill assumption.
2. Add estimated entry fee, stop-exit fee, slippage, and other defensible stop scenario costs.
3. Round quantity down to `volUnit`; reject if below `minVol`, above `maxVol`, or if rounding breaks the risk budget.
4. Verify initial/maintenance margin and liquidation-price buffer. Leverage is an outcome of quantity and margin rules, never a desired multiplier.
5. Record **gross planned R geometry** separately from all-in risk cash and **net realized R**. Define the denominator once in the backtest and paper broker. A practical convention is: risk budget is the maximum modeled stop-out loss including planned stop costs; realized R = realized net P&L / that initial risk budget. Compute post-cost reward/risk consistently and never subtract the same cost twice.

### Cost model

- Use the latest published API schedule as the initial non-Innovation-Zone assumption: maker 0.06% and taker 0.08% per charged leg; confirm actual account/contract pricing later.
- First baseline entry is next 1H open and modeled taker. Assume taker fees for exits unless order/fill evidence supports a passive maker fill; never award maker fees just because a target is drawn as a limit.
- Apply historical funding at actual eligible settlement timestamps using verified MEXC funding history. Validate the coverage and rate conventions before modeling it.
- Use tiered, conservative slippage estimates by liquidity and intended notional, with explicit spread and gap stress. Because historical book depth/OI are not confirmed, do not invent them in backtests.
- Publish base, 1.5x and 2.0x cost stress results. Keep an assumption ledger. If the strategy only works with optimistic costs, reject it.
- A cost-in-R veto and a post-cost reward/risk veto must be mathematically compatible with the target R. Avoid stacking a fixed 1.5R target, a very tight cost cap and an arbitrarily high net-RR threshold that makes every valid trade impossible.

### Provisional future pilot settings

These settings are for research discussion only and require explicit approval before any live use: approximately 0.10%–0.20% of equity risk per trade during a supervised tiny pilot; a low cap on aggregate open risk; a small number of concurrent positions; same-direction altcoins treated as one correlated crypto-beta bucket; and written daily/weekly drawdown stops. Thresholds must be selected based on account equity, contract minimums, drawdown tolerance and the strategy's tail distribution, not copied mechanically from a PDF. There is no live trading in this plan.

---

## 8. Data validation and anti-look-ahead rules

### First probe: public data only

Query a sample consisting of BTC_USDT, ETH_USDT and one liquid altcoin, over enough history to cover many weeks. Inspect current official API paths and actual payloads. Check:

- 4H timestamps fall on observed exchange boundaries and 1D boundary semantics are understood.
- Timestamp units are consistent (the kline page describes Unix seconds; signed requests use milliseconds).
- There are no silent missing/duplicate bars or unexplained irregular intervals.
- REST and WebSocket `open` / `realOpen` fields, if present, are compared and recorded; do not infer meanings from variable names alone.
- Last price, fair price and index price series are understood separately. The documented order endpoint permits stop/take-profit trigger-price types: latest (1), fair (2), index (3). That does not by itself establish which is the correct research fill series; it must be aligned to the planned trigger type.
- `contractSize`, tick/volume precision, min/max volume, state, API allowance and risk tiers are captured.
- Funding history includes the expected timestamps and values.
- Kline pagination respects the 2,000-point response cap and the documented 20 calls / 2 seconds limit. Start conservatively below the limit (e.g., 70% request budget) with endpoint-specific rate limiters.

### Causal feature invariant

For every decision time T, compute the features from only data with `available_at <= T`. A truncation/property test removes all data after T and asserts the features and candidate at T are bit-for-bit unchanged. Do this for 1D, 12H, 4H and 1H. Historical corrections must create a versioned dataset; do not silently rewrite previously evaluated inputs.

### Missing data

For a new candidate, missing required bars means NO_TRADE. Do not fill candles by interpolation. If a simulated open position encounters a data void, report the event as a gap/stress scenario and avoid silently assuming the stop was or was not hit. Keep conservative and excluded/ambiguous results separately.

### Survivorship

Snapshot the eligible contract list forward from the project start. When earlier history for delisted symbols is unavailable, say so; do not claim perfect point-in-time universe reconstruction. Evaluate results by symbol and contract vintage where possible.

---

## 9. Backtesting, accuracy and statistical acceptance

“Accuracy” is not one percentage. Win rate, direction accuracy, profitability and reliability answer different questions. Main economic metric is cost-adjusted expectancy, while risk and operational metrics are co-equal acceptance dimensions.

### Required metrics for each run

- Symbol universe, candles loaded/valid/missing, data version and configuration hash.
- Candidate count, accepted setup count, trade count, rejection counts by reason, long/short split.
- Win rate with Wilson interval; average winner/loser; mean and median net R; net expectancy and uncertainty interval; profit factor.
- Maximum drawdown, recovery duration, largest loss, tail-loss quantiles and loss streaks.
- Gross P&L vs fee, funding, spread and slippage decomposition; unfilled/rejected order count; holding time and time exposed.
- Return/equity with explicit start capital, position sizing, leverage/margin assumptions.
- Per-symbol, regime, calendar period and long/short breakdown; development vs validation vs locked OOS.
- Paper-versus-backtest replay divergence and execution assumptions versus any measured fills later.

### Fill simulation rules

- Candidate is known only after completed 1H candle. Market entry occurs at the next 1H open plus conservative slippage; never fill at the signal close by default.
- Limit orders count as filled only under a predeclared price-through rule. A candle touching a limit does not prove queue execution. Report non-fills and missed opportunities.
- If stop and target are both touched in one 1H candle and candle data cannot establish the path, assume the adverse stop first and label the ambiguity. Add a sensitivity report for the ambiguous set.
- Stop gaps fill at a worse plausible executable price, not the exact stop unconditionally.
- Partial exits, funding settlement, quantity rounding, minimum size, margin/liquidation and order rejects must be modeled explicitly if included in system scope.
- Report missing-data trades under separate conservative/excluded policies. Never let an unknown trade disappear without a count.

### Validation design

1. **Development:** build the hypotheses and debug features; these data may be inspected repeatedly.
2. **Validation / walk-forward:** chronological expanding training and fixed forward blocks; use sensitivity, ablation and baseline comparison only here.
3. **Frozen experiment manifest:** hash the strategy code/config, data version, cost model, metric definitions, search space and rejection criteria. Record every tried variant.
4. **Locked final holdout:** an untouched chronological period, evaluated once after freezing. A failed or inconclusive holdout is not repaired by tweaking against that same period.
5. **Forward paper:** prospective data after freeze; check implementation correctness and divergence, not just total R.

Use DSR/PBO or similar multiple-testing diagnostics where implementation is defensible, but neither replaces honest chronology and a locked holdout. Use block/stationary bootstrap for uncertainty and drawdown/sequence risk. Independent random shuffling of each trade may destroy dependence and understate clustering. Monte Carlo paths are re-samples or scenarios of the same evidence, not additional independent trades.

### Sample-size warning

If standard deviation of trade R is around 1.2R and trades were IID, the standard error of mean R is 1.2 / sqrt(n): approximately 0.12R at n=100 and 0.06R at n=400; the corresponding rough 95% half-widths are ±0.24R and ±0.12R. Real trades share time and crypto-beta exposure, so dependence makes effective sample size smaller. A 100-trade paper run detects operational failures or a very poor model; it does not establish a small positive edge.

### Promotion gates (provisional, not universal proof)

- **G0 — Data/engineering correctness:** alignment fixtures, candle aggregation, causality/truncation, deterministic replay, costs/sizing, contract precision and all failure-path tests pass. No fabricated data or unlimited-liquidity fallback.
- **G1 — Research evidence:** one pre-registered locked OOS test has positive cost-stressed net expectancy with a dependence-aware lower confidence bound above the predeclared economic hurdle; results are not concentrated in one coin/regime and survive 1.5x friction. Targets such as 300–350 trades, three regimes, 40–50 symbols or DSR >=0.95 are provisional criteria to pre-register—not magic numbers. If the sample cannot support the conclusion, the result is inconclusive, not a pass.
- **G2 — Forward paper:** at least eight continuous weeks and an adequate number of resolved observations (100 is a minimum diagnostic floor, not proof of edge), no unresolved ledger states, data gaps correctly halting new signals, and same-period paper results reconciled against backtest replay under matching assumptions.
- **G3 — Optional supervised tiny live pilot:** not authorized by this plan. Requires explicit human approval, account/regional eligibility confirmation, static-IP/key restrictions as applicable, tested order-state transitions, verified protective stops, kill/flatten procedures, fault injection, daily reconciliation and tiny predeclared risk.
- **G4 — Any broader automation:** considered only after pilot evidence, repeated audits and written ongoing kill criteria. Never enabled by WhatsApp text or an AI response.

---

## 10. “Time Machine” / simulation module

Keep six outputs distinct:

1. Historical event-driven backtest: the core strategy evidence.
2. Trade execution simulation: fills, fees, funding, spread/slippage, limits, partials, gaps and order rejects.
3. Sensitivity and ablation: small predeclared ranges and one-family-at-a-time tests; no peak-shopping.
4. Walk-forward / untouched OOS: tests generalization.
5. Block-bootstrap / Monte Carlo: drawdown, equity-path and loss-streak distribution under stated assumptions.
6. Scenario stress: fees up 1.5x/2x, slippage spikes, funding extremes, data outage, order timeout, correlated altcoin loss cluster and stop gap.

Dashboard should show equity, drawdown/time-under-water, net expectancy distribution, cost waterfall, symbol/regime table, parameter stability and rejection reasons. Every chart must state what it does and does not prove. Never headline “10,000 simulations” as if they were 10,000 independent real trades. Monte Carlo cannot save a strategy that fails locked OOS.

---

## 11. Paper trading, ledger and eventual order safety

### Paper broker

It consumes live public market data but sends no orders. Its fill model must clearly label assumed fills and use the same fees, funding, stop/target and position-accounting functions as backtests. Store immutable events for decision, candidate, order intent, simulated acceptance/rejection, fill assumptions, position changes, funding, fees, P&L, equity, open risk and errors.

### Future order-state machine

```text
INTENT_CREATED -> INTENT_PERSISTED -> SUBMISSION_ATTEMPTED
  -> ACKNOWLEDGED -> OPEN / PARTIALLY_FILLED -> FILLED
  -> PROTECTION_PENDING -> PROTECTED -> EXIT_PENDING -> CLOSED -> RECONCILED

Any response may lead to REJECTED or UNKNOWN.
UNKNOWN -> RECONCILIATION_REQUIRED -> query exchange orders, deals and positions
             -> re-establish authoritative state before any retry or new entry.
```

The `externalOid` should be written to durable storage before the network call and used for reconciliation, but must not be assumed to guarantee exactly-once processing. A network timeout after submission is an unknown outcome, not permission to send again. Verify stop/take-profit state by querying the exchange. A field in the request or an HTTP success response is not proof that protection is active.

On exchange/data outage, halt new entries. If a live position exists, keep running the independent reconciliation and risk-management loop and raise an out-of-band alert. Do not use an unverified internal position snapshot to fire repeated close orders. Risk-reducing actions must be tested for mode, `reduceOnly` behavior, partial fills and ambiguous responses.

---

## 12. WhatsApp interface and security

### Initial commands

- `HELP`: list safe commands.
- `STATUS`: mode, service health, data freshness and last successful scan.
- `MARKET <symbol>`: latest validated multi-timeframe summary, with candle timestamps.
- `SCAN` / `SIGNALS`: candidate and rejection summary; not an order command.
- `PERFORMANCE`: explicitly labelled backtest/paper metrics, source period and limitations.
- `RISK`: current simulated exposure and hard limits.
- `PAUSE`: stop new paper candidates and/or entries; do not silently forget any paper/live position state.
- `LIVE_STATUS`: read-only state; live remains off.

`RESUME` may be limited to restarting paper scanning after an authorized operator confirmation. There should be **no WhatsApp command that enables live execution, changes the risk budget, alters strategy parameters or sends a real order**. A later capability would require a separate security and authorization design, not just an additional command.

### Security rules

- Official WhatsApp Cloud API. Verify webhook challenge on setup; validate `X-Hub-Signature-256` HMAC-SHA256 over raw request bytes before JSON parsing; constant-time comparison.
- Sender allowlist and role-based authorization; deduplicate message IDs; reject stale/replayed messages; rate-limit commands.
- Parse commands with deterministic grammar. Treat text, forwarded messages, symbols, exchange payloads and AI input as untrusted. AI never authenticates, authorizes or parses control commands.
- Audit sender, timestamp, command, authorization decision and result. Avoid leaking stack traces, account details, API identifiers or secrets in chat.
- Message delivery failure does not suspend backend safety. Use a separate uptime/alert channel as out-of-band monitoring.
- Keep alerts high-value and batched; verify current Meta rate-card and provider fees before deployment.

### Optional AI layer (late, provider-neutral)

Only after core stability, an optional explainer may answer questions using read-only, typed tool results from the decision ledger, e.g. “why was this candidate rejected?” Inputs must be source-tagged and time-stamped. Output must use a schema and be validated. No tools for orders, balances writes, risk configuration, command authorization or live enablement. If the provider is down, outputs are malformed or inputs are stale, the system falls back to deterministic stored explanations.

---

## 13. Reliability, observability and security operations

Required metrics include data age per symbol/timeframe; number of missing/duplicated candles; 12H builder rejects; REST errors and rate-limit responses; WebSocket lag/reconnects; scheduler lag/overlap; number of candidates and first-fail veto reasons; daily signal count outside expected bands; ledger write failures; paper/backtest divergence; modeled costs versus any future observed costs; WhatsApp delivery failures; restart recovery mismatches; clock drift; and memory growth.

Required mechanisms:

- Endpoint-specific rate limiter below published limits (a conservative initial budget can be ~70% of limits, not a protocol guarantee).
- Bounded timeout and exponential backoff with jitter, circuit breaker and explicit `NO_TRADE` after repeated data failure. No infinite retry loops.
- WebSocket heartbeat/reconnect, planned rotation before connection expiry where applicable, subscription restoration and REST backfill for gaps.
- Single-flight scheduler and persistent run IDs; no overlapping scan creates duplicate candidates.
- Clock synchronization and signed-request drift check before private API activity.
- Durable storage, backup/restore drill, schema/version migration and replayable events.
- Kill switch and recovery runbook; deploy rollbacks version both code and config.
- Separate system health from WhatsApp health; never let a messaging outage hide a stopped scanner.

---

## 14. What to build, delay or reject

### Build first

- Public-only MEXC adapter and contract/schema tests.
- Validated candle store, historical loader and pagination.
- Deterministic 12H builder and causal feature engine.
- One baseline H1 hypothesis with config fingerprint and full candidate/rejection logging.
- Contract-aware fee/funding/slippage/sizing library.
- Event-driven backtester plus tests for path ambiguity, fills and accounting.
- Locked experiment registry and out-of-sample report.
- Paper broker and append-only ledger.
- Read-only WhatsApp status/report service and independent monitoring.

### Delay until core research passes

- Complex score optimization.
- Simultaneous strategy portfolios (H2/H3) and correlation optimization.
- AI explainer.
- Advanced visualization/“Time Machine” front end.
- Live API key, private account integration and executor.
- Dynamic strategy adaptation or online learning.

### Reject

- 5M/15M/30M or any other candle-based strategy trigger.
- Assuming a native 12H MEXC candle exists.
- Treating the stock-market 5-minute ORB demonstration as a strategy specification.
- Any claimed accuracy/win rate not backed by auditable locked OOS results.
- Simulation count as proof of edge.
- Blind order retries, silent fallback data, unlimited liquidity, assumed perfect fills or “protective order accepted therefore active.”
- AI-driven risk changes, natural-language command execution, autonomous strategy self-modification or live toggles through WhatsApp.
- A fixed volume/spread threshold, mandatory indicator stack, or composite score chosen solely to produce more trades.
- Coding multiple variants and then reporting only the winner without a full trial log.

---

## 15. Phased implementation roadmap (plan only)

### Phase 0 — Resolve data truth

**Work:** run a public unauthenticated probe on BTC_USDT, ETH_USDT and one liquid altcoin. Verify timestamp boundaries, `open`/`realOpen` behavior if available, REST/WS consistency, fair/index/latest price distinctions, candle gaps, kline pagination, contract metadata, `apiAllowed`, funding-history coverage and the effective fee bulletin. Commit raw sample fixtures and a decision memo.

**Acceptance:** written evidence for candle alignment and series semantics; all unresolved facts explicitly marked; no trading key; no orders.

### Phase 1 — Data foundation

**Work:** build adapter, loader, immutable raw-data store, canonical candle store, universe snapshots, data validator and 12H aggregator.

**Acceptance:** no unflagged data gaps; duplicate handling deterministic; 12H uses exactly three valid 4H children; correction/version handling tested; all causality properties pass.

### Phase 2 — Baseline strategy and experiment harness

**Work:** implement causal pivots and H1 as a single hypothesis, risk geometry and candidate/rejection ledger; establish baseline comparison and registered alternative parameter values.

**Acceptance:** independent tests prove completed bars only; exact replay yields identical candidates; each config displays its actual runtime values and config hash; no indicators or parameters quietly diverge from labels.

### Phase 3 — Cost-aware backtester

**Work:** contract-aware P&L, order size/precision, costs, funding, entry/exit timing, stops/targets, gaps, ambiguous same-bar exits, unfilled orders, min-size rejects and portfolio risk accounting.

**Acceptance:** unit/property tests for cashflows and rounding; cost waterfall and 1.5x/2x stress; a frozen run is reproducible. No “perfect fill” shortcut.

### Phase 4 — Pre-registration and locked OOS

**Work:** document hypotheses, all prior trials, train/validation/OOS chronology, primary metric and uncertainty method, cost model, threshold rules and stop criteria. Lock the OOS data and config before running it once.

**Acceptance:** G1 decision is pass/fail/inconclusive with raw ledger, uncertainty estimates and explanation. Failure means stop/research redesign using development data; no parameter adjustments against the same holdout.

### Phase 5 — Paper system and WhatsApp read-only interface

**Work:** forward paper broker, position and cost ledger, scheduled reports, secure webhook commands, uptime monitoring and replay/dedupe.

**Acceptance:** at least eight weeks of continuous paper operation and sufficient resolved observations; paper replay aligns with the backtest under the same assumptions; no unresolved ledger state; all stale-data cases halt new candidates.

### Phase 6 — Live-readiness study only

**Work only after explicit instruction and G0-G2 evidence:** evaluate MEXC account/KYC/regional API eligibility, current contract/account fees, static IP/key policy, stop/TP trigger semantics, position mode/margin mode, external order ID queries, order state behavior and failure recovery. Use supervised operational drills; no autonomous setting switch.

**Acceptance:** written human-approved checklist, protected risk limits, verified cancel/close/stop paths, fault-injection drills, independent kill switch and successful reconciliation. G3 is not authorized by this document.

### Phase 7 — Capped operation, if ever approved

Only after a supervised pilot is proven. Maintain tiny risk, hard exposure limits, ongoing live-vs-paper discrepancy alerts, weekly reviews, and an automatic stop if fees, market rules, slippage or operational behavior change. Any broader operation needs separate written authorization and a new review.

---

## 16. Decisions to make later (do not block Phase 0)

1. Which market universe size is sustainable after data validation and cost-based liquidity filters?
2. Which small threshold set will be pre-registered for 4H impulse, pullback depth, stop buffer, fixed target and maximum holding time?
3. What capital and maximum drawdown should inform simulated sizing and any eventual pilot? These cannot be inferred from reports.
4. Which state-changing WhatsApp actions, if any beyond PAUSE, are actually required? Default: none.
5. Is an AI explainer useful after the deterministic system has proven stable? Default: not in the MVP.
6. Which language/framework/storage/host best meets the project constraints? Decide with comparative evidence later, not by habit or because a report recommended a vendor.

---

## 17. Immediate next action: alignment and semantics probe

This is the only action to do next. It is cheap, non-destructive and unlocks the whole research stack.

**Probe output should include:**

- endpoint and access timestamp;
- exact REST request/response fixtures and schema version;
- UTC conversion plus observed 4H and 1D start-time distributions across many weeks;
- 12H aggregation examples, including missing/duplicate child bars;
- differences between `open` and `realOpen` if available, and separate last/fair/index series;
- actual current contract metadata and `apiAllowed` for the chosen sample;
- funding-history record coverage, cadence and timestamp semantics;
- documented rate limits and pagination proof;
- current published fee notice and any unresolved account-specific fee question;
- pass/fail status for every assumption, plus test fixtures to use later.

**Stop condition:** if the time alignment or price/trigger semantics remain unresolved, do not proceed to strategy tuning. Keep the system at data research only and resolve the ambiguity first.

---

## 18. Independent sources checked for this plan

1. MEXC, **Updates to API Futures Trading Fees (Jun 1, 2026)**, published May 28, effective June 1, 2026. Confirms 0.06% maker / 0.08% taker. https://www.mexc.com/announcements/article/updates-to-api-futures-trading-fees-jun-1-2026-17827791535742
2. MEXC, **Introducing API Futures Trading on Mar 31, 2026**, published March 30, 2026. Older 0.01% / 0.05% launch schedule, superseded by source 1. https://www.mexc.com/announcements/article/introducing-api-futures-trading-on-mar-31-2026-17827791534551
3. MEXC Futures API, **Get Candlestick Data**. Documents endpoint, intervals, 20 calls / 2 seconds and maximum 2,000 points. https://www.mexc.com/api-docs/futures/market-endpoints/get-candlestick-data
4. MEXC Futures API, **Get Contract Info**. Documents the currently displayed `/api/v1/contract/detail/country` path and contract precision/eligibility fields. https://www.mexc.com/api-docs/futures/market-endpoints/get-contract-info
5. MEXC Futures API, **Integration Guide**. Documents API URL, signing, request-time window, KYC and IP binding/90-day expiry. https://www.mexc.com/api-docs/futures/integration-guide
6. MEXC Futures API, **Change Log**. Records the January 19, 2026 REST base-domain migration and March 31, 2026 reopening of Futures order access. https://www.mexc.com/api-docs/futures/update-log
7. MEXC, **API page / FAQ**. Says API uses live environment and no API sandbox/test environment is currently offered. https://www.mexc.com/mexc-api
8. MEXC Futures API, **Place Order**. Documents order type, `externalOid`, stop/take-profit fields and trigger price type. https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order
9. Bailey & López de Prado, **The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality**, Journal of Portfolio Management (2014). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
10. Bailey et al., **The Probability of Backtest Overfitting**, Journal of Computational Finance (2017). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
11. WhatsApp Business Platform pricing references for October 2026 were not independently retrieved directly from Meta in this session due access throttling; current secondary reproductions indicate the Pakistan utility/service rate is approximately $0.015 per delivered message. Verify the official rate card and provider fees before launch. Secondary cross-check: https://karobar.io/tools/whatsapp-api-pricing-calculator/pakistan

---

## Final recommendation

Start with the public-data alignment/semantics probe and a written evidence log. Then build the validated data layer, one small falsifiable strategy, a contract-aware cost model and an honest backtester. Only after a locked out-of-sample result and forward paper audit should WhatsApp automation broaden; the AI explainer and live executor are optional late-stage components. If the strategy fails, stop or redesign on development data. Never loosen controls, select a variant from the final holdout, or treat a polished user interface as proof of profitability.

**Planning status:** complete.  
**Implementation status:** not started.  
**Live execution:** disabled.
