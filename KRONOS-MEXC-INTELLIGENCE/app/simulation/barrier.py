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
                  slippage_bps: float = 0.0, risk_per_unit: float | None = None) -> PathOutcome:
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    risk = risk_per_unit or (entry - stop if side == "LONG" else stop - entry)
    if risk <= 0:
        raise ValueError("Invalid entry/stop geometry")
    tp1_seen = False
    for i, row in enumerate(path, start=1):
        _, high, low, close = map(float, row[:4])
        if side == "LONG":
            stop_hit, tp1_hit, tp2_hit = low <= stop, high >= tp1, high >= tp2
        else:
            stop_hit, tp1_hit, tp2_hit = high >= stop, low <= tp1, low <= tp2
        any_target = tp1_hit or tp2_hit
        if stop_hit and any_target:
            if tp1_seen:
                # TP1 was already realized on an earlier bar; the remaining half
                # is conservatively stopped when TP2 and stop are both touched.
                effective_exit = 0.5 * tp1 + 0.5 * stop
                gross_r = 0.5 * 1.0 + 0.5 * -1.0
                return PathOutcome("TP1_FIRST", i, gross_r - _cost_r(entry, effective_exit, fee_rate, slippage_bps, risk))
            return PathOutcome("AMBIGUOUS_CONSERVATIVE_STOP", i, -1.0 - _cost_r(entry, stop, fee_rate, slippage_bps, risk))
        if stop_hit:
            if tp1_seen:
                # Illustrative 50% TP1 realization and 50% stop on remainder.
                gross_r = 0.5 * 1.0 + 0.5 * -1.0
                effective_exit = 0.5 * tp1 + 0.5 * stop
                return PathOutcome("TP1_FIRST", i, gross_r - _cost_r(entry, effective_exit, fee_rate, slippage_bps, risk))
            return PathOutcome("SL_FIRST", i, -1.0 - _cost_r(entry, stop, fee_rate, slippage_bps, risk))
        if tp2_hit:
            gross_r = 0.5 * 1.0 + 0.5 * 2.0
            effective_exit = 0.5 * tp1 + 0.5 * tp2
            return PathOutcome("TP2_REACHED_BEFORE_SL", i, gross_r - _cost_r(entry, effective_exit, fee_rate, slippage_bps, risk))
        if tp1_hit:
            tp1_seen = True
    last_close = float(path[-1, 3])
    remaining_r = (last_close - entry) / risk if side == "LONG" else (entry - last_close) / risk
    gross_r = 0.5 * 1.0 + 0.5 * remaining_r if tp1_seen else remaining_r
    outcome = "TP1_FIRST" if tp1_seen else "NEITHER"
    exit_price = last_close
    effective_exit = 0.5 * tp1 + 0.5 * exit_price if tp1_seen else exit_price
    return PathOutcome(outcome, None, gross_r - _cost_r(entry, effective_exit, fee_rate, slippage_bps, risk))


def _cost_r(entry: float, exit_price: float, fee_rate: float, slippage_bps: float, risk: float) -> float:
    # Price-proportional approximation per unit, round trip, for a linear contract.
    total_cost = (entry + exit_price) * (max(fee_rate, 0) + max(slippage_bps, 0) / 10_000)
    return total_cost / risk


def evaluate_paths(paths: np.ndarray, side: str, entry: float, stop: float,
                   tp1: float, tp2: float, *, fee_rate: float,
                   slippage_bps: float) -> dict:
    data = np.asarray(paths, dtype=float)
    if data.ndim != 3 or data.shape[-1] < 4 or not np.isfinite(data).all():
        raise ValueError("Expected finite path matrix [n_paths, horizon, >=4]")
    outcomes = [race_one_path(p, side, entry, stop, tp1, tp2, fee_rate=fee_rate,
                              slippage_bps=slippage_bps) for p in data]
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
        "mean_net_r": float(values.mean()),
        "median_net_r": float(np.median(values)),
        "p05_net_r": float(np.quantile(values, 0.05)),
        "p95_net_r": float(np.quantile(values, 0.95)),
        "positive_share": float((values > 0).mean()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and abs(losses.sum()) else None,
        "interpretation": "Simulation frequencies are not calibrated market probabilities.",
    }
