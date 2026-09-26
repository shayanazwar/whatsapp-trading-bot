from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from .risk_manager import (
    TradePlan,
    calculate_rr,
    validate_levels,
)
from .setup_filter import validate_analysis


MIN_SCORE = 82
MIN_RR = 2.0
MIN_CONFIRMATION_FAMILIES = 5

FIVE_MINUTE_MS = 300_000
FIFTEEN_MINUTE_MS = 900_000
DEFAULT_MAX_SIGNAL_AGE_MS = 330_000


@dataclass(frozen=True)
class ValidatedSignal:
    key: str
    symbol: str
    side: str
    candle_time: int
    analysis: dict[str, Any]
    plan: TradePlan


def make_signal_key(
    symbol: str,
    side: str,
    candle_time: int,
) -> str:
    raw = (
        f"mexc|{symbol.upper()}|"
        f"{side.upper()}|{int(candle_time)}"
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:40]


def _ms(value: Any) -> int | None:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return None

    return v * 1000 if v < 10**12 else v


def validate_signal(
    data: dict[str, Any],
    *,
    min_confluence: int,
    min_rr: float,
    require_increasing_volume: bool,
) -> tuple[ValidatedSignal | None, list[str]]:

    # ---------------------------------------------------------
    # PRIMARY ANALYSIS VALIDATION
    # ---------------------------------------------------------

    ok, reasons = validate_analysis(
        data,
        min_confluence=min_confluence,
        min_rr=min_rr,
        require_increasing_volume=require_increasing_volume,
    )

    if not ok:
        return None, reasons

    # ---------------------------------------------------------
    # SIDE
    # ---------------------------------------------------------

    side = str(data["setup"]).upper()

    if side not in {"LONG", "SHORT"}:
        return None, ["Invalid LONG/SHORT setup"]

    # ---------------------------------------------------------
    # CANDLE TIMESTAMPS
    # ---------------------------------------------------------

    fifteen = _ms(
        data.get("candle_time")
    )

    five = _ms(
        data.get("closed_5m_candle_time")
    )

    if fifteen is None or five is None:
        return None, [
            "Missing normalized candle timestamps"
        ]

    if five < fifteen:
        return None, [
            "5M trigger belongs to an earlier 15M candle"
        ]

    if (
        five - fifteen
        > FIFTEEN_MINUTE_MS + FIVE_MINUTE_MS
    ):
        return None, [
            "5M trigger is stale relative to 15M setup"
        ]

    if five % FIVE_MINUTE_MS != 0:
        return None, [
            "5M trigger timestamp is not 5M-aligned"
        ]

    # ---------------------------------------------------------
    # SIGNAL FRESHNESS
    # ---------------------------------------------------------

    now = int(time.time() * 1000)

    max_age = int(
        float(
            data.get(
                "max_signal_age_seconds",
                DEFAULT_MAX_SIGNAL_AGE_MS / 1000,
            )
            or DEFAULT_MAX_SIGNAL_AGE_MS / 1000
        )
        * 1000
    )

    if five > now + FIVE_MINUTE_MS:
        return None, [
            "5M trigger timestamp is in the future"
        ]

    if now - five > max_age:
        return None, [
            f"5M trigger age exceeds "
            f"{max_age / 1000:.0f}s"
        ]

    # ---------------------------------------------------------
    # TRADE LEVELS
    # ---------------------------------------------------------

    try:
        entry = float(data["entry"])
        stop_loss = float(data["stop_loss"])
        tp1 = float(data["tp1"])
        tp2 = float(data["tp2"])
    except (KeyError, TypeError, ValueError):
        return None, [
            "Invalid or missing trade levels"
        ]

    # ---------------------------------------------------------
    # INDEPENDENT RR CALCULATION
    #
    # IMPORTANT:
    # Do NOT trust data["rr"].
    # RR is recalculated from Entry / SL / TP2.
    # ---------------------------------------------------------

    try:
        calculated_rr = calculate_rr(
            side=side,
            entry=entry,
            stop_loss=stop_loss,
            target=tp2,
        )
    except (TypeError, ValueError) as exc:
        return None, [
            f"Invalid trade levels: {exc}"
        ]

    # ---------------------------------------------------------
    # TRADE PLAN
    # ---------------------------------------------------------

    plan = TradePlan(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        rr=calculated_rr,
    )

    # ---------------------------------------------------------
    # FINAL LEVEL + RR VALIDATION
    # ---------------------------------------------------------

    level_ok, level_reason = validate_levels(
        plan,
        min_rr=max(
            MIN_RR,
            float(min_rr),
        ),
    )

    if not level_ok:
        return None, [level_reason]

    # ---------------------------------------------------------
    # SYMBOL
    # ---------------------------------------------------------

    symbol = str(
        data.get("symbol") or ""
    ).strip()

    if not symbol:
        return None, ["Missing symbol"]

    # ---------------------------------------------------------
    # SIGNAL KEY
    # ---------------------------------------------------------

    key = make_signal_key(
        symbol,
        side,
        five,
    )

    # ---------------------------------------------------------
    # VALIDATED SIGNAL
    # ---------------------------------------------------------

    return (
        ValidatedSignal(
            key=key,
            symbol=symbol.upper(),
            side=side,
            candle_time=five,
            analysis=data,
            plan=plan,
        ),
        [],
    )
