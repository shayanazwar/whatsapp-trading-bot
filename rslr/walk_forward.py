from __future__ import annotations

from datetime import datetime
from typing import Any

from rslr.backtest import BacktestTrade, calculate_metrics, run_backtest


def walk_forward_from_result(result: Any, in_sample_fraction: float = 0.70) -> dict[str, Any]:
    """Chronologically split an already-computed fixed-strategy run.

    No parameters are fitted or optimized. The OOS period is strictly later than the
    in-sample period. Trades are assigned by entry timestamp to avoid shuffling.
    """
    if not 0.5 <= in_sample_fraction < 0.9:
        raise ValueError("in_sample_fraction must be between 0.50 and 0.90")
    start = datetime.fromisoformat(result.start)
    end = datetime.fromisoformat(result.end)
    boundary = start + (end - start) * in_sample_fraction
    in_sample: list[BacktestTrade] = []
    out_sample: list[BacktestTrade] = []
    for trade in result.trades:
        entry = datetime.fromisoformat(trade.entry_time)
        (in_sample if entry < boundary else out_sample).append(trade)
    in_resolved = [t for t in in_sample if t.result in {"TP", "SL", "EXPIRED"}]
    in_equity = result.starting_equity + sum(t.net_pnl for t in in_resolved)
    train_metrics = calculate_metrics(
        "IN_SAMPLE", start, boundary, result.starting_equity, in_sample, result.coins_tested
    )
    test_metrics = calculate_metrics(
        "OUT_OF_SAMPLE", boundary, end, in_equity, out_sample, result.coins_tested
    )
    return {
        "period": result.period,
        "split_fraction": in_sample_fraction,
        "boundary": boundary.isoformat(),
        "in_sample": train_metrics.to_dict(),
        "out_of_sample": test_metrics.to_dict(),
        "warning": "Fixed parameters only; no parameter tuning was performed. OOS sample may be too small for inference.",
    }


def run_walk_forward(period: str = "30D") -> dict[str, Any]:
    if period.upper() not in {"30D"}:
        raise ValueError("Walk-forward mode currently uses a 30D chronological window")
    result = run_backtest(period)
    return walk_forward_from_result(result)
