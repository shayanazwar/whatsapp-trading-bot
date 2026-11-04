from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PathOutcome:
    outcome: str
    bars_to_event: int | None
    net_r: float


def race_one_path(path: np.ndarray, side: str, entry: float, stop: float,
                  tp1: float, tp2: float, *, fee_rate: float = 0.0,
                  slippage_bps: float = 0.0, risk_per_unit: float | None = None,
                  tp1_exit_fraction: float = 0.5) -> PathOutcome:
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    direction = 1.0 if side == "LONG" else -1.0
    risk = risk_per_unit or (entry - stop if side == "LONG" else stop - entry)
    if risk <= 0:
        raise ValueError("Invalid entry/stop geometry")
    if not np.isfinite(tp1_exit_fraction) or not 0.0 < tp1_exit_fraction < 1.0:
        raise ValueError("tp1_exit_fraction must be finite and in (0, 1)")
    f1, f2 = tp1_exit_fraction, 1.0 - tp1_exit_fraction
    r_tp1 = direction * (tp1 - entry) / risk
    r_tp2 = direction * (tp2 - entry) / risk
    tp1_seen = False
    for i, row in enumerate(path, start=1):
        op, high, low, close = map(float, row[:4])
        if side == "LONG":
            stop_hit, tp1_hit, tp2_hit = low <= stop, high >= tp1, high >= tp2
            stop_fill = min(stop, op)  # stop-market gaps fill at adverse open
        else:
            stop_hit, tp1_hit, tp2_hit = high >= stop, low <= tp1, low <= tp2
            stop_fill = max(stop, op)
        any_target = tp1_hit or tp2_hit
        if stop_hit and any_target:
            if tp1_seen:
                gross_r = f1 * r_tp1 + f2 * direction * (stop_fill - entry) / risk
                effective_exit = f1 * tp1 + f2 * stop_fill
                return PathOutcome("TP1_FIRST", i, gross_r - _cost_r(entry, effective_exit, fee_rate, slippage_bps, risk))
            stop_r = direction * (stop_fill - entry) / risk
            return PathOutcome("AMBIGUOUS_CONSERVATIVE_STOP", i,
                               stop_r - _cost_r(entry, stop_fill, fee_rate, slippage_bps, risk))
        if stop_hit:
            if tp1_seen:
                gross_r = f1 * r_tp1 + f2 * direction * (stop_fill - entry) / risk
                effective_exit = f1 * tp1 + f2 * stop_fill
                return PathOutcome("TP1_FIRST", i, gross_r - _cost_r(entry, effective_exit, fee_rate, slippage_bps, risk))
            stop_r = direction * (stop_fill - entry) / risk
            return PathOutcome("SL_FIRST", i, stop_r - _cost_r(entry, stop_fill, fee_rate, slippage_bps, risk))
        if tp2_hit:
            gross_r = f1 * r_tp1 + f2 * r_tp2
            effective_exit = f1 * tp1 + f2 * tp2
            return PathOutcome("TP2_REACHED_BEFORE_SL", i, gross_r - _cost_r(entry, effective_exit, fee_rate, slippage_bps, risk))
        if tp1_hit and not tp1_seen:
            tp1_seen = True
    if len(path) == 0:
        raise ValueError("A forecast path must contain at least one future bar")
    last_close = float(path[-1, 3])
    remaining_r = direction * (last_close - entry) / risk
    gross_r = f1 * r_tp1 + f2 * remaining_r if tp1_seen else remaining_r
    outcome = "TP1_FIRST" if tp1_seen else "NEITHER"
    effective_exit = f1 * tp1 + f2 * last_close if tp1_seen else last_close
    return PathOutcome(outcome, None, gross_r - _cost_r(entry, effective_exit, fee_rate, slippage_bps, risk))


def _cost_r(entry: float, exit_price: float, fee_rate: float, slippage_bps: float, risk: float) -> float:
    total_cost = (entry + exit_price) * (max(fee_rate, 0) + max(slippage_bps, 0) / 10_000)
    return total_cost / risk


def evaluate_paths(paths: np.ndarray, side: str, entry: float, stop: float,
                   tp1: float, tp2: float, *, fee_rate: float,
                   slippage_bps: float, tp1_exit_fraction: float = 0.5) -> dict:
    data = np.asarray(paths, dtype=float)
    if data.ndim != 3 or data.shape[-1] < 4 or not np.isfinite(data).all():
        raise ValueError("Expected finite path matrix [n_paths, horizon, >=4]")
    if data.shape[0] == 0 or data.shape[1] == 0:
        raise ValueError("At least one non-empty path is required")
    outcomes = [race_one_path(p, side, entry, stop, tp1, tp2, fee_rate=fee_rate,
                              slippage_bps=slippage_bps,
                              tp1_exit_fraction=tp1_exit_fraction) for p in data]
    counts = Counter(x.outcome for x in outcomes)
    total = len(outcomes)
    values = np.array([x.net_r for x in outcomes], dtype=float)
    wins = values[values > 0]
    losses = values[values < 0]
    return {
        "paths": total,
        "outcomes": {k: {"count": int(v), "share": float(v / total)} for k, v in sorted(counts.items())},
        "tp1_first": counts.get("TP1_FIRST", 0),
        "tp2_reached_before_sl": counts.get("TP2_REACHED_BEFORE_SL", 0),
        "sl_first": counts.get("SL_FIRST", 0),
        "neither": counts.get("NEITHER", 0),
        "ambiguous": counts.get("AMBIGUOUS_CONSERVATIVE_STOP", 0),
        # Funding is intentionally not guessed from current rates. Every result
        # is after modeled entry/exit fees and slippage, but BEFORE funding.
        "mean_r_after_fees_slippage_before_funding": float(values.mean()),
        "median_r_after_fees_slippage_before_funding": float(np.median(values)),
        "p05_r_after_fees_slippage_before_funding": float(np.quantile(values, 0.05)),
        "p95_r_after_fees_slippage_before_funding": float(np.quantile(values, 0.95)),
        "positive_share": float((values > 0).mean()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and abs(losses.sum()) else None,
        "gap_aware_stop_fill": True,
        "tp1_exit_fraction": tp1_exit_fraction,
        "tp2_exit_fraction": 1.0 - tp1_exit_fraction,
        "funding_included": False,
        "cost_basis": "AFTER_ESTIMATED_FEES_AND_SLIPPAGE_BEFORE_FUNDING",
        "interpretation": "Simulation frequencies are not calibrated market probabilities; funding is excluded.",
    }
