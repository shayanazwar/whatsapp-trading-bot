from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .risk_manager import TradePlan, validate_levels
from .setup_filter import validate_analysis


@dataclass(frozen=True)
class ValidatedSignal:
    key: str
    symbol: str
    side: str
    candle_time: int
    analysis: dict[str, Any]
    plan: TradePlan


def make_signal_key(symbol: str, side: str, candle_time: int) -> str:
    raw = f"mexc|{symbol.upper()}|{side}|{int(candle_time)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def validate_signal(
    data: dict[str, Any],
    *,
    min_confluence: int,
    min_rr: float,
    require_increasing_volume: bool,
) -> tuple[ValidatedSignal | None, list[str]]:
    valid, reasons = validate_analysis(
        data,
        min_confluence=min_confluence,
        min_rr=min_rr,
        require_increasing_volume=require_increasing_volume,
    )
    if not valid:
        return None, reasons

    candle_time = data.get("candle_time")
    if candle_time is None:
        return None, ["15M closed candle timestamp is missing"]

    plan = TradePlan(
        side=str(data["setup"]),
        entry=float(data["entry"]),
        stop_loss=float(data["stop_loss"]),
        tp1=float(data["tp1"]),
        tp2=float(data["tp2"]),
        rr=float(data["rr"]),
    )
    levels_ok, level_reason = validate_levels(plan, min_rr)
    if not levels_ok:
        return None, [level_reason]

    return (
        ValidatedSignal(
            key=make_signal_key(data["symbol"], plan.side, int(candle_time)),
            symbol=str(data["symbol"]),
            side=plan.side,
            candle_time=int(candle_time),
            analysis=dict(data),
            plan=plan,
        ),
        [],
    )
