# Pak Trading Academy — Independent SMC Engine (SMC-1.0)

## Runtime / safety

- The original V11 engine remains the default. Select the separate research engine with `ANALYSIS_ENGINE=SMC`; restore the baseline with `ANALYSIS_ENGINE=V11`.
- Scanner and historical runner use the same engine selector. This does not enable orders. Keep `AUTO_TRADE_ENABLED=false` and `ALLOW_LIVE_EXECUTION=false`.
- Decision data is restricted to completed 1D, 12H, 4H and 1H candles. Entries are signals at the closed 1H candle; the existing simulator models execution from the next 1H candle.
- 12H uses the existing UTC-aligned aggregator: exactly three contiguous completed 4H bars; open=first open, high=max high, low=min low, close=last close, volume=sum. Incomplete groups are rejected.

## Objective rules

1. **Swing points:** two candles on the left and two on the right. A pivot becomes usable only after the second right-hand candle closes. The engine stores its occurrence index and knowable index internally.
2. **Structure:** HH/HL when the latest confirmed swing high and low both exceed their prior counterparts; LH/LL when both are lower; otherwise range/unknown. Daily regime is a 2-of-3 directional vote: structure, close vs EMA50, and EMA50 vs EMA200. If bull and bear votes tie or neither reaches two, no trade.
3. **12H context:** a strongly opposing 12H structure plus price on the wrong side of EMA50 is a scoring penalty/context warning, not an automatic veto.
4. **4H dealing range:** latest confirmed high and low known at decision time; equilibrium is their midpoint. Discount is below 50%; premium is above 50%. This is a heuristic current range, not a claim about institutional intent.
5. **Fair Value Gap:** bullish when candle A high < candle C low, zone=[A.high,C.low]; bearish when A.low > C.high, zone=[C.high,A.low]. Later completed bars determine whether it remains untouched, was entered/partially mitigated, was fully filled, or was invalidated by a close beyond the far boundary. A simple candle gap is not called order-book imbalance.
6. **Order block:** the last opposing-colour candle within six candles before a close through a confirmed swing level. Bullish zone=[low,open]; bearish zone=[open,high]. A later close beyond the far boundary invalidates it. This is a deterministic OHLCV proxy, not proof that institutional orders exist there.
7. **Liquidity:** the engine estimates a pool using the lowest low / highest high of the three prior completed 1H candles. A sweep must breach that level. A later completed 1H candle must reclaim beyond the level and beyond the sweep candle's opposite extreme, with directional body quality and close-location checks. Actual stops and actual resting liquidity are not observable from OHLCV.
8. **POI / location:** either discount for a potential LONG or premium for a potential SHORT, or a recent active OB/FVG retest, is required alongside the directional regime and 4H structure.
9. **Entry:** market-reference entry is the confirming 1H close. The common paper simulator models the following 1H open and adverse slippage.
10. **Stop:** sweep extreme plus buffer `max(0.20 × ATR(4H), 0.15 × ATR(1H))`. LONG stop must be below entry; SHORT stop above entry.
11. **Target:** nearest confirmed 4H/12H/1D swing target beyond entry; 4H dealing-range boundary is a fallback candidate. The engine will not jump to a farther swing simply to inflate RR. Net RR subtracts configured round-trip cost estimate and must be at least 1.60R.
12. **Shock/data vetoes:** insufficient closed history, malformed ordering, unavailable ATR, invalid geometry, an oversized 1H range (>4.5× ATR1H), no structural target, or net RR below the floor means no trade. Shared project-level BTC and execution-quality checks remain part of the scanner path.
13. **Scoring:** 8 diagnostic groups (HTF, structure, liquidity event, candle-body displacement proxy, OB/FVG, premium/discount, entry confirmation, target quality). The score is not independently evidence-calibrated yet; do not present it as a probability of winning.

## SMC concept inventory / implementation matrix

Status refers to this implementation, not whether the general concept can be useful in another system. A=core rule; B=supporting score; C=context/reporting; D=not observable/available enough; E=redundant, subjective or not independently used.

| Concept(s) | Objective definition used or reason | TF | Status / role | Validation required |
|---|---|---|---|---|
| HH, HL, LH, LL; swing high/low; bullish/bearish structure; range/consolidation | Confirmed 2-left/2-right pivots; compare latest two highs and lows | 1D/12H/4H | A: regime and directional structure | Pivot timing; synthetic monotonic sequences |
| BOS | A completed close through a swing only after its right-side confirmation time | 4H; 1H reclaim uses a local swept-level break | A/B: OB origin and entry confirmation | No early pivot / no future candle tests |
| CHoCH, MSS, internal/external structure, protected/strong/weak highs/lows | Not emitted as distinct, independent gates; would overlap swing/BOS events without an independently calibrated definition | 1D/12H/4H/1H | E/C: structure diagnostics only | Separate labels would need causal event-state tests |
| Dealing range, premium, discount, equilibrium (50%) | Range from latest confirmed 4H swing high/low; midpoint | 4H | A: location gate and context | Causality and correct boundary selection |
| BSL, SSL, EQH, EQL, inferred liquidity pool | Current trigger estimates local liquidity from prior 3 1H highs/lows; no explicit EQH/EQL tolerance clustering yet | 1H | A: local sweep proxy; C: actual pools remain estimates | Long/short mirrored sweep fixtures; tolerance study |
| Sweep, grab, stop hunt, stop-loss cluster, IRL/ERL, IDM, DOL, resting liquidity, raid, sweep/reclaim | Only sweep/reclaim is operational; other names do not prove actual stops or intent | 1H trigger, HTF context | A for sweep/reclaim; C/D/E for the rest | Track sweep and reclaim separately; never infer real stops |
| Bullish/bearish OB; mitigation; invalidation; failed OB | Opposing candle before close through confirmed swing; far-boundary close invalidates | 4H | A/B: POI and score | Create, retest, invalidation fixtures; out-of-sample ablation |
| Breaker, mitigation block, rejection/propulsion/refined OB | No distinct validated lifecycle; generic OB proxy only | 4H | D/E | Do not claim implemented; need independent objective definitions |
| Bullish/bearish FVG; mitigation; full fill; invalidation | Three-candle inequality and following completed bars | 4H | A/B: POI status and score | Lifecycle state fixtures; gap/no-gap tests |
| IFVG, BPR, CE, VI, liquidity void, candle imbalance, rebalancing | No fully qualified independent calculation in the trade gate; no order-book imbalance inference | 4H/1H | D/C | Independent rules and data tests needed |
| Supply/demand, support/resistance, flip/role reversal, rejection zone, POI | Confirmed swing/OB/FVG boundaries; POI is a context label | 4H/12H/1D | B/C | Zone lifecycle and overlapping-zone de-duplication |
| OTE, Fibonacci retracement, PD arrays, nested OB/FVG | Not used as a mandatory rule; no validated evidence that these improve this sample | HTF | D/E | Test one feature at a time on held-out data |
| Displacement, impulse/correction, expansion/retracement | Current scoring uses directional candle-body/range quality as a proxy; no claim about institutional delivery | 4H/1H | B | Compare proxy against ATR-normalized alternatives |
| CISD, price delivery, accumulation/reaccumulation, redistribution, manipulation/distribution | Not objectively distinguished by this engine | 1D/12H/4H/1H | D/E | Require explicit, reproducible event definitions first |
| Bull/bear traps, fakeout, false/failed breakout, breakout retest | A confirmed sweep/reclaim overlaps the measurable subset; losing breakouts are not labeled manipulation | 1H | B/E | Outcome-independent labels and test fixtures |
| ICT Unicorn, 2022 model, Market Maker models, Silver Bullet, AMD/PO3, Turtle Soup | Not implemented as named models; would duplicate features or depend on subjective/session assumptions | 1D/12H/4H/1H | D/E | Separate falsifiable definitions required |
| Asian range, London/NY sessions, kill zones, session high/low, opening range | No intrahour/session-special dependency is added; session labels are not part of current decision | 1H+ | D/C | UTC boundaries and coverage tests before any future use |
| Daily/weekly/monthly/midnight open; NDOG/NWOG | Daily candle opens are available but these are not strategy gates; weekly/monthly gap models not implemented | 1D+ | C/D | Instrument-specific UTC convention tests |
| HTF bias, top-down analysis, MTF alignment, IRL/ERL/DOL, PD alignment | Uses 1D regime, 12H context, 4H structure/location, 1H trigger; not all timeframes need perfect agreement | 1D→12H→4H→1H | A | Side-separated diagnostics and regime splits |
| SMT/intermarket divergence, IPDA, external comparisons, institutional flow | No reliable timestamp-aligned cross-market/order-flow source is required by this engine | All | D | Must be sourced and aligned before use |
| Entry, SL/TP, net RR, expiry, risk, trade invalidation | Market reference from 1H close; structural stop; nearest HTF target; common simulator | 1H with HTF target | A | Geometry, fees, fill assumptions, SL-first same-bar handling |
| Partial TP, breakeven, max simultaneous positions, funding attribution | Existing simulator handles its own defined trade lifecycle; SMC currently emits one final structural TP and has no SMC-specific partial-exit model | 1H | D/C | Explicitly align trade-management policies before comparing |

## Known limitations

- SMC-1.0 has not yet earned a claim of higher accuracy or profitability. Historical 1D/3D/7D/30D out-of-sample comparisons are required.
- A simple `CONTROL-1.0` comparator is included for matched backtests: 1D EMA50/200 trend + 4H EMA21/50 alignment + a completed 1H three-bar breakout, with a structural stop and nearest confirmed HTF swing target. It is selectable only through `BACKTEST_ENGINE` and is not used by the scanner.
- The current 1H liquidity threshold is a short-horizon OHLCV proxy; it is not exchange order-book/stop data.
- Funding, precise spread, and actual executable depth are not guaranteed to exist in historical candles. The backtest cost model must report those as limitations when absent.
- Strategy selection defaults to V11 to avoid silently replacing a previously tested signal path with an unvalidated strategy. `ANALYSIS_ENGINE=SMC` selects SMC for scanner research only. `BACKTEST_ENGINE=SMC` or `BACKTEST_ENGINE=CONTROL` overrides only the historical simulator; it does not change the scanner selection or enable live execution. `CONTROL` is a simple EMA-trend + 1H breakout comparator, not an SMC strategy.
