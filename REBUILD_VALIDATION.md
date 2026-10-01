# MEXC Intraday Engine + Multi-Stage Simulator Rebuild

## Strategy behavior implemented

- **4H:** market regime. Confirmed 4H trend can trade in its direction. A neutral/sideways 4H regime may trade only when the 1H direction is exceptionally clear (4/4 directional votes).
- **1H:** directional bias + structure.
- **15M:** BOS/retest defines the intraday setup. A separate 15M entry candle is supportive, not a hidden hard gate.
- **5M:** immediate execution trigger. A valid trigger requires continuation BOS + strong candle + expanding volume + directional momentum.
- **Stop:** structural invalidation (15M/1H structural anchors) plus ATR buffer. No fixed price-percentage stop floor/cap.
- **Targets:** real structural/liquidity levels. TP1 is the nearest meaningful obstacle; TP2 is a higher-timeframe structural level that clears the required 2R reward. No fixed 1–2% price target floor and no artificial 3R ceiling.
- **Signal frequency:** thresholds were loosened to pursue more legitimate opportunities without fabricating setups; 10+ signals/day is a target when market structure actually provides them, not a hard quota.

## Simulator state machine

`OPEN -> TP1_PARTIAL -> TP2_FINAL`

`OPEN -> TP1_PARTIAL -> BREAKEVEN`

`OPEN -> STOP_FINAL`

`OPEN -> EXPIRED`

At TP1, exactly 50% of the **initial** position is closed once. The remaining 50% uses a breakeven stop at the executable entry. The original stop is permanently inactive after TP1. TP2 or breakeven closes the full residual position.

Same-bar ambiguity is configurable with `SL_FIRST` (default, pessimistic) or `TP_FIRST`.

Position-size validation rejects configurations where an exact 50% close cannot be represented by the supplied lot/volume step. The simulator never rounds the partial exit into a different position size.

Fees and adverse slippage are accounted for separately, while realized R is calculated from actual realized net PnL against initial full-position structural risk.

Invalid OHLC rows are skipped safely. Missing future candles return `None` instead of inventing an exit. Finalized trades always have zero residual position size.

## Exact files changed/added

### Replacements

- `app/analysis/engine.py`
  - Reworked trend gating for 4H/1H alignment.
  - Made 15M BOS/retest the setup definition and 5M the mandatory immediate execution trigger.
  - Added strict 5M continuation BOS + strong candle + volume-expansion trigger.
  - Removed arbitrary fixed stop/target percentage floors and the 3R TP2 cap.
  - Stop placement is structural with ATR safety bounds/buffer only.
  - TP1/TP2 come from structural levels across 15M/1H/4H/1D.

- `app/automation/scanner.py`
  - Uses the same 4H/1H/15M directional/setup gates as the engine.
  - Removes the old mandatory 15M entry-candle gate.
  - Requires the rebuilt 5M continuation trigger.
  - Live geometry validation now uses structural ordering, entry drift, ATR stop sanity and RR only; no fixed stop/target percentage floor.
  - Keeps a compatibility import for `_fifteen_minute_entry_confirmation` without using it as a hard gate.

- `app/automation/setup_filter.py`
  - Matches the rebuilt engine thresholds.
  - Treats 15M as setup structure and 5M as the mandatory execution trigger.
  - Removes EMA-extension and fixed stop-percentage hard gates.

- `app/backtest/simulator.py`
  - Complete TP1 -> breakeven -> TP2 state machine.
  - Exact 50% initial-position TP1 close and exact residual close.
  - Breakeven stop activation after TP1; original SL cannot trigger afterward.
  - Configurable same-bar `SL_FIRST` / `TP_FIRST` behavior.
  - Correct partial realized PnL, fees, slippage, and R accounting.
  - Lot/contract-step validation for exact 50% exits.
  - Defensive input validation and no orphan residual state.

- `app/backtest/report.py`
  - Adds TP1->BE reporting.
  - Uses realized net R from the new simulator.
  - Treats finalized EXPIRED trades as resolved when realized R exists.
  - Updates quality buckets/wording for the rebuilt thresholds and 15M/5M semantics.

### Tests

- `tests/test_backtest_feature.py`
  - Updates legacy simulator expectations to the correct 50%/50% accounting.
  - Updates report expectations accordingly.

- `tests/test_intraday_engine.py`
  - Replaces fixed-percent-stop and 3R-cap expectations with structural-only behavior.

- `tests/test_multi_stage_intraday_rebuild.py`
  - New synthetic coverage for TP2, TP1->BE, same-bar rules, position sizing, costs, malformed candles, expiry safety, trend gating, 5M trigger requirements, and live geometry.

## Validation performed

- Original ZIP baseline: **42 tests passed**.
- Final rebuilt repository: **58 tests passed**.
- `python -m compileall -q app tests`: **PASS**.
- Synthetic simulator batch: **4,000 scenarios**, including deterministic TP2/BE/SL paths plus fuzzed OHLC inputs; **0 exceptions and 0 position-state invariant failures**.
- Separate deterministic expiry batch: **500 scenarios**, finalized with zero residual position size.
- Key financial check: entry 100 / SL 95 / TP1 106 / TP2 110 produces **+1.60R gross** when TP1 and TP2 both complete (50% at each target), not +2R.
- TP1 followed by breakeven produces **+0.60R gross** before fees/slippage for the same geometry.

A live MEXC historical backtest was not executed from this ZIP because the archive does not contain the external historical market dataset/credentials required to reproduce one offline. Use the repository's existing `BACKTEST 1D`, `BACKTEST 7D`, `BACKTEST 30D`, or `BACKTEST 90D` command after integration.

## Integration

Copy the files in this bundle to the exact relative paths above, replacing only those files. Do not replace `app/backtest/runner.py` or `app/automation/executor.py`; the runner already calls `simulate_trade`, and live execution remains hard-disabled in the supplied codebase.

Then run:

```bash
python -m compileall -q app tests
python -m pytest -q
```

After deployment/configuration with MEXC access, run the repository's backtest command, for example:

```text
BACKTEST 1D
BACKTEST 7D
```

Compare TP1, TP1->BE, TP2, SL, expectancy and Total R between runs. The simulator's actual realized R now reflects the staged 50%/50% exits.
