"""Deterministic research hypothesis S1-RFPC-0.2.0-1H-trigger.

Rules are provisional and have NOT been validated for profitability.
1D EMA regime -> 12H confirmed swing structure -> 4H breakout/retest/reclaim -> 1H trigger.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Mapping, Sequence

from app.market.candles import Candle, STRATEGY_TIMEFRAMES, assert_strategy_timeframes
from app.market.indicators import atr, confirmed_pivots, ema

STRATEGY_VERSION = "S1-RFPC-0.2.0-1H-trigger"


class StrategyInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TradeCandidate:
    candidate_id: str
    strategy_version: str
    symbol: str
    side: str
    decision_time: int
    entry_reference: float
    stop_price: float
    target_price: float
    initial_risk_distance: float
    regime: str
    setup_pivot_time: int
    breakout_time: int
    reclaim_time: int
    trigger_time: int
    gross_reward_risk: float
    reason: str
    evidence: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    symbol: str
    decision_time: int
    regime: str
    candidate: TradeCandidate | None
    rejection_reasons: tuple[str, ...]
    evidence: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "decision_time": self.decision_time,
            "regime": self.regime,
            "candidate": self.candidate.as_dict() if self.candidate else None,
            "rejection_reasons": list(self.rejection_reasons),
            "evidence": dict(self.evidence),
        }


def _closes(bars: Sequence[Candle]) -> list[float]:
    return [c.close for c in bars]


def _ohlc(bars: Sequence[Candle]) -> tuple[list[float], list[float], list[float]]:
    return [c.high for c in bars], [c.low for c in bars], [c.close for c in bars]


def _filter_closed(series: Mapping[str, Sequence[Candle]], decision_time: int) -> dict[str, list[Candle]]:
    unknown = set(series) - STRATEGY_TIMEFRAMES
    if unknown:
        assert_strategy_timeframes(series.keys())
    missing = STRATEGY_TIMEFRAMES - set(series)
    if missing:
        raise StrategyInputError(f"Missing required timeframe(s): {', '.join(sorted(missing))}")
    filtered: dict[str, list[Candle]] = {}
    symbols: set[str] = set()
    for tf in STRATEGY_TIMEFRAMES:
        bars = sorted(series[tf], key=lambda c: c.open_time)
        if any(c.timeframe != tf for c in bars):
            raise StrategyInputError(f"Mixed or incorrectly-labelled bars in {tf}")
        if any(c.close_time > decision_time or not c.complete for c in bars):
            bars = [c for c in bars if c.complete and c.close_time <= decision_time]
        seen: set[int] = set()
        deduped: list[Candle] = []
        for c in bars:
            if c.open_time in seen:
                raise StrategyInputError(f"Duplicate {tf} candle at {c.open_time}")
            seen.add(c.open_time)
            deduped.append(c)
            symbols.add(c.symbol)
        filtered[tf] = deduped
    if len(symbols) > 1:
        raise StrategyInputError("All timeframe series must refer to exactly one symbol")
    return filtered


def _regime(daily: Sequence[Candle]) -> tuple[str, dict[str, object]]:
    closes = _closes(daily)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    if not closes or ema50[-1] is None or ema200[-1] is None:
        return "INSUFFICIENT_DATA", {"daily_bars": len(daily), "ema50": None, "ema200": None}
    c, e50, e200 = closes[-1], float(ema50[-1]), float(ema200[-1])
    if c > e50 > e200:
        regime = "LONG"
    elif c < e50 < e200:
        regime = "SHORT"
    else:
        regime = "NEUTRAL"
    return regime, {"daily_bars": len(daily), "daily_close": c, "ema50": e50, "ema200": e200}


def _structure_direction(bars12: Sequence[Candle]) -> tuple[str, dict[str, object]]:
    highs, lows, _ = _ohlc(bars12)
    pivot_highs, pivot_lows = confirmed_pivots(highs, lows, 2)
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return "AMBIGUOUS", {
            "confirmed_swing_highs": len(pivot_highs), "confirmed_swing_lows": len(pivot_lows),
        }
    h0, h1 = pivot_highs[-2], pivot_highs[-1]
    l0, l1 = pivot_lows[-2], pivot_lows[-1]
    if highs[h1] > highs[h0] and lows[l1] > lows[l0] and bars12[-1].close >= lows[l1]:
        state = "LONG"
    elif highs[h1] < highs[h0] and lows[l1] < lows[l0] and bars12[-1].close <= highs[h1]:
        state = "SHORT"
    else:
        state = "AMBIGUOUS"
    return state, {
        "confirmed_swing_highs": len(pivot_highs),
        "confirmed_swing_lows": len(pivot_lows),
        "last_two_swing_high_prices": [highs[h0], highs[h1]],
        "last_two_swing_low_prices": [lows[l0], lows[l1]],
        "latest_12h_close": bars12[-1].close,
    }


def _find_active_4h_setup(bars4: Sequence[Candle], side: str) -> dict[str, float | int] | None:
    """Find the most recent unexpired completed 4H breakout/retest/reclaim setup."""
    if len(bars4) < 25:
        return None
    highs, lows, closes = _ohlc(bars4)
    atrs = atr(highs, lows, closes, 14)
    pivot_highs, pivot_lows = confirmed_pivots(highs, lows, 2)
    pivots = pivot_highs if side == "LONG" else pivot_lows
    patterns: list[dict[str, float | int]] = []

    # An active setup can only survive a finite chain: pivot confirmation (2 bars),
    # breakout, retest within 6 bars, reclaim within 3 more, and expiry 3 bars later.
    # A 24-bar tail is therefore sufficient to find any currently active setup while
    # avoiding repeated quadratic rescans of the entire history on every 1H decision.
    active_setup_lookback = 24
    earliest_pivot_index = max(0, len(bars4) - active_setup_lookback)
    # A returned setup must reclaim within the last 4 bars (expiry <= 3),
    # with retest at most 3 bars before reclaim and breakout at most 6 bars
    # before retest. Therefore no breakout earlier than len(bars4)-13 can
    # possibly be active at the current right edge. The old unbounded loop
    # spent most of its time evaluating expired historical setups repeatedly.
    earliest_possible_breakout = max(0, len(bars4) - 13)
    last_index = len(bars4) - 1
    for p in pivots:
        if p < earliest_pivot_index:
            continue
        confirmation_index = p + 2
        breakout_start = max(confirmation_index + 1, earliest_possible_breakout)
        for breakout_idx in range(breakout_start, len(bars4)):
            a_idx = breakout_idx - 1
            if a_idx < 13 or atrs[a_idx] is None or float(atrs[a_idx]) <= 0:
                continue
            a = float(atrs[a_idx])
            pivot_price = highs[p] if side == "LONG" else lows[p]
            breakout_valid = closes[breakout_idx] > pivot_price + 0.10 * a if side == "LONG" else closes[breakout_idx] < pivot_price - 0.10 * a
            if not breakout_valid:
                continue
            retest_idx: int | None = None
            invalidated = False
            for j in range(breakout_idx + 1, min(breakout_idx + 6 + 1, len(bars4) - 0)):
                if side == "LONG":
                    if closes[j] < pivot_price - 0.25 * a:
                        invalidated = True
                        break
                    if lows[j] <= pivot_price + 0.25 * a and closes[j] >= pivot_price - 0.25 * a:
                        retest_idx = j
                        break
                else:
                    if closes[j] > pivot_price + 0.25 * a:
                        invalidated = True
                        break
                    if highs[j] >= pivot_price - 0.25 * a and closes[j] <= pivot_price + 0.25 * a:
                        retest_idx = j
                        break
            if invalidated or retest_idx is None:
                continue
            reclaim_idx: int | None = None
            for k in range(retest_idx + 1, min(retest_idx + 3 + 1, len(bars4))):
                if side == "LONG":
                    valid = closes[k] > pivot_price and closes[k] > highs[k - 1]
                    broken = closes[k] < pivot_price - 0.25 * a
                else:
                    valid = closes[k] < pivot_price and closes[k] < lows[k - 1]
                    broken = closes[k] > pivot_price + 0.25 * a
                if broken:
                    break
                if valid:
                    reclaim_idx = k
                    break
            if reclaim_idx is None:
                continue
            # Expired reclaims can never be returned, whether invalidated or not;
            # skip their post-reclaim scan entirely. For live candidates, the scan
            # is bounded to at most 3 subsequent bars by the expiry rule.
            if last_index - reclaim_idx > 3:
                continue
            post_invalid = False
            for q in range(reclaim_idx + 1, len(bars4)):
                if side == "LONG" and closes[q] < pivot_price - 0.25 * a:
                    post_invalid = True
                    break
                if side == "SHORT" and closes[q] > pivot_price + 0.25 * a:
                    post_invalid = True
                    break
            if post_invalid:
                continue
            all_lows = lows[breakout_idx : reclaim_idx + 1]
            all_highs = highs[breakout_idx : reclaim_idx + 1]
            stop = min(all_lows) - 0.25 * a if side == "LONG" else max(all_highs) + 0.25 * a
            patterns.append({
                "pivot_index": p,
                "pivot_time": bars4[p].open_time,
                "pivot_price": pivot_price,
                "breakout_index": breakout_idx,
                "breakout_time": bars4[breakout_idx].open_time,
                "retest_index": retest_idx,
                "retest_time": bars4[retest_idx].open_time,
                "reclaim_index": reclaim_idx,
                "reclaim_time": bars4[reclaim_idx].close_time,
                "atr_before_breakout": a,
                "stop": stop,
            })
    if not patterns:
        return None
    return max(patterns, key=lambda x: int(x["reclaim_time"]))


def evaluate_strategy(
    symbol: str,
    series: Mapping[str, Sequence[Candle]],
    *,
    decision_time: int | None = None,
    strategy_version: str = STRATEGY_VERSION,
    prevalidated: bool = False,
    target_r_multiple: float = 2.0,
) -> StrategyEvaluation:
    """Evaluate one symbol at one timestamp using completed bars only.

    ``prevalidated`` is an internal performance hook for the backtester. It may
    only be used when all series are already sorted, unique, complete, closed
    by ``decision_time``, and validated for symbol/timeframe. All public callers
    retain the defensive filtering/validation path by default.
    """
    if not isinstance(target_r_multiple, (int, float)) or not 0.25 <= float(target_r_multiple) <= 10:
        raise ValueError("target_r_multiple must be between 0.25R and 10R")
    target_r_multiple = float(target_r_multiple)
    unknown = set(series) - STRATEGY_TIMEFRAMES
    if unknown:
        assert_strategy_timeframes(series.keys())
    missing = STRATEGY_TIMEFRAMES - set(series)
    if missing:
        raise StrategyInputError(f"Missing required timeframe(s): {', '.join(sorted(missing))}")
    if not series.get("1H"):
        return StrategyEvaluation(symbol, decision_time or 0, "INSUFFICIENT_DATA", None, ("NO_1H_DATA",), {})
    if decision_time is None:
        decision_time = max(c.close_time for c in series["1H"] if c.complete)
    if prevalidated:
        # BacktestRunner has already sorted, removed incomplete rows, rejected
        # duplicate opens, checked symbol/timeframe labels, and bisected each
        # timeframe to close_time <= decision_time. Do not repeat that work on
        # every hourly decision.
        bars = {tf: series[tf] for tf in STRATEGY_TIMEFRAMES}
    else:
        bars = _filter_closed(series, int(decision_time))
    for tf in STRATEGY_TIMEFRAMES:
        if not bars[tf]:
            return StrategyEvaluation(symbol, int(decision_time), "INSUFFICIENT_DATA", None, (f"NO_CLOSED_{tf}_DATA",), {})
    if not prevalidated and any(c.symbol != symbol for values in bars.values() for c in values):
        raise StrategyInputError("Requested symbol does not match one or more input time series")

    regime, daily_evidence = _regime(bars["1D"])
    if regime == "INSUFFICIENT_DATA":
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("EMA_WARMUP_INSUFFICIENT",), daily_evidence)
    structure, structure_evidence = _structure_direction(bars["12H"])
    evidence: dict[str, object] = {"1D": daily_evidence, "12H": structure_evidence, "strategy_version": strategy_version}
    if regime == "NEUTRAL":
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("DAILY_REGIME_NEUTRAL",), evidence)
    if structure != regime:
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("TWELVE_HOUR_STRUCTURE_MISALIGNED",), evidence)

    bars4, bars1 = bars["4H"], bars["1H"]
    if len(bars4) < 25 or len(bars1) < 10:
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("SETUP_WARMUP_INSUFFICIENT",), evidence)
    latest1 = bars1[-1]
    trigger_idx = len(bars1) - 1
    if trigger_idx < 3:
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("TRIGGER_WARMUP_INSUFFICIENT",), evidence)
    side = regime
    setup = _find_active_4h_setup(bars4, side)
    if setup is None:
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("NO_ACTIVE_4H_BREAKOUT_RETEST_RECLAIM",), evidence)
    reclaim_time = int(setup["reclaim_time"])
    if latest1.open_time < reclaim_time:
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("1H_TRIGGER_PRECEDES_RECLAIM",), evidence)
    if latest1.close_time > reclaim_time + 12 * 3600:
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("1H_TRIGGER_WINDOW_EXPIRED",), evidence)

    prior3 = bars1[-4:-1]
    trigger_ok = latest1.close > max(c.high for c in prior3) if side == "LONG" else latest1.close < min(c.low for c in prior3)
    if not trigger_ok:
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("NO_1H_CONFIRMATION",), evidence)
    entry_ref = latest1.close
    stop = float(setup["stop"])
    if (side == "LONG" and stop >= entry_ref) or (side == "SHORT" and stop <= entry_ref):
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("STOP_NOT_ON_LOSS_SIDE",), evidence)
    risk_dist = abs(entry_ref - stop)
    target = entry_ref + (target_r_multiple * risk_dist if side == "LONG" else -target_r_multiple * risk_dist)
    # Keep setup qualification fixed at the control's 2R path. TP sensitivity runs
    # must compare the same entry candidates and change exits only.
    control_path_target = entry_ref + (2.0 * risk_dist if side == "LONG" else -2.0 * risk_dist)
    highs12, lows12, _ = _ohlc(bars["12H"])
    ph12, pl12 = confirmed_pivots(highs12, lows12, 2)
    if side == "LONG":
        obstacles = [highs12[i] for i in ph12 if highs12[i] > entry_ref and highs12[i] <= control_path_target]
    else:
        obstacles = [lows12[i] for i in pl12 if lows12[i] < entry_ref and lows12[i] >= control_path_target]
    if obstacles:
        return StrategyEvaluation(symbol, int(decision_time), regime, None, ("OPPOSING_12H_LEVEL_BLOCKS_2R_TARGET",), {**evidence, "opposing_levels": obstacles})

    candidate_key = f"{symbol}|{side}|{setup['pivot_time']}|{setup['breakout_time']}|{setup['reclaim_time']}|{latest1.open_time}|{strategy_version}"
    candidate_id = hashlib.sha256(candidate_key.encode()).hexdigest()[:20].upper()
    candidate = TradeCandidate(
        candidate_id=candidate_id,
        strategy_version=strategy_version,
        symbol=symbol,
        side=side,
        decision_time=int(decision_time),
        entry_reference=float(entry_ref),
        stop_price=stop,
        target_price=float(target),
        initial_risk_distance=float(risk_dist),
        regime=regime,
        setup_pivot_time=int(setup["pivot_time"]),
        breakout_time=int(setup["breakout_time"]),
        reclaim_time=reclaim_time,
        trigger_time=latest1.open_time,
        gross_reward_risk=target_r_multiple,
        reason="1D regime and 12H structure aligned; 4H breakout/retest/reclaim active; 1H trigger confirmed.",
        evidence={
            **evidence,
            "1H_trigger_close": latest1.close,
            "previous_three_1H_extreme": max(c.high for c in prior3) if side == "LONG" else min(c.low for c in prior3),
            "4H_pivot_price": float(setup["pivot_price"]),
            "4H_atr_before_breakout": float(setup["atr_before_breakout"]),
            "entry_is_not_a_fill": True,
            "next_1H_open_required_for_backtest_fill": True,
        },
    )
    return StrategyEvaluation(symbol, int(decision_time), regime, candidate, (), evidence)
