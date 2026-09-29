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


MIN_SCORE = 90
MIN_RR = 2.0
MIN_CONFIRMATION_FAMILIES = 6

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
    except (
        TypeError,
        ValueError,
    ):
        return None

    return (
        v * 1000
        if v < 10**12
        else v
    )


def validate_signal(
    data: dict[str, Any],
    *,
    min_confluence: int,
    min_rr: float,
    require_increasing_volume: bool,
) -> tuple[
    ValidatedSignal | None,
    list[str],
]:

    # =========================================================
    # PRIMARY ANALYSIS VALIDATION
    #
    # setup_filter.py owns the technical gates:
    # score, RR, families, engine flags, BTC filter,
    # target path, volatility, etc.
    # =========================================================

    ok, reasons = validate_analysis(
        data,
        min_confluence=min_confluence,
        min_rr=min_rr,
        require_increasing_volume=require_increasing_volume,
    )

    if not ok:
        return None, reasons

    # =========================================================
    # SIDE
    # =========================================================

    side = str(
        data.get("setup")
        or ""
    ).upper()

    if side not in {
        "LONG",
        "SHORT",
    }:
        return None, [
            "Invalid LONG/SHORT setup"
        ]

    # =========================================================
    # SYMBOL
    # =========================================================

    symbol = str(
        data.get("symbol")
        or ""
    ).strip()

    if not symbol:
        return None, [
            "Missing symbol"
        ]

    symbol = symbol.upper()

    # =========================================================
    # CANDLE TIMESTAMPS
    #
    # The engine's primary 15M setup uses the latest closed 5M candle only
    # as a freshness clock when 15M is the authoritative entry timeframe.
    # =========================================================

    fifteen = _ms(
        data.get("candle_time")
    )

    five = _ms(
        data.get(
            "closed_5m_candle_time"
        )
    )

    if fifteen is None:
        return None, [
            "Missing normalized 15M candle timestamp"
        ]

    primary_tf = str(data.get("primary_entry_timeframe") or "5M").upper()

    if five is None:
        return None, [
            "Missing normalized 5M freshness timestamp"
        ]

    if primary_tf == "15M":
        if five + FIVE_MINUTE_MS < fifteen:
            return None, [
                "5M freshness timestamp is older than the primary 15M setup"
            ]
        if five - fifteen > FIFTEEN_MINUTE_MS + FIVE_MINUTE_MS:
            return None, [
                "5M freshness timestamp is stale relative to 15M setup"
            ]
    else:
        if five < fifteen:
            return None, [
                "5M trigger belongs to an earlier 15M candle"
            ]
        if five - fifteen > FIFTEEN_MINUTE_MS + FIVE_MINUTE_MS:
            return None, [
                "5M trigger is stale relative to 15M setup"
            ]

    if five % FIVE_MINUTE_MS != 0:
        return None, [
            "5M trigger timestamp is not 5M-aligned"
        ]

    # =========================================================
    # SIGNAL FRESHNESS
    # =========================================================

    now = int(
        time.time() * 1000
    )

    configured_age_seconds = data.get(
        "max_signal_age_seconds"
    )

    try:
        max_age = int(
            float(
                configured_age_seconds
                if configured_age_seconds is not None
                else DEFAULT_MAX_SIGNAL_AGE_MS / 1000
            )
            * 1000
        )
    except (
        TypeError,
        ValueError,
    ):
        max_age = DEFAULT_MAX_SIGNAL_AGE_MS

    # Future timestamp protection.
    if five > now + FIVE_MINUTE_MS:
        return None, [
            "5M trigger timestamp is in the future"
        ]

    signal_age = now - five

    if signal_age > max_age:
        return None, [
            f"5M trigger age exceeds "
            f"{max_age / 1000:.0f}s"
        ]

    if signal_age < 0:
        return None, [
            "5M trigger timestamp is unexpectedly in the future"
        ]

    # =========================================================
    # TRADE LEVELS
    # =========================================================

    try:
        entry = float(
            data["entry"]
        )

        stop_loss = float(
            data["stop_loss"]
        )

        tp1 = float(
            data["tp1"]
        )

        tp2 = float(
            data["tp2"]
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ):
        return None, [
            "Invalid or missing trade levels"
        ]

    # Basic numerical sanity.
    if (
        entry <= 0
        or stop_loss <= 0
        or tp1 <= 0
        or tp2 <= 0
    ):
        return None, [
            "Trade levels must be positive"
        ]

    # =========================================================
    # SIDE-SPECIFIC LEVEL VALIDATION
    #
    # Do a small independent sanity check before constructing
    # the final TradePlan.
    # =========================================================

    if side == "LONG":
        if stop_loss >= entry:
            return None, [
                "LONG stop loss must be below entry"
            ]

        if tp1 <= entry:
            return None, [
                "LONG TP1 must be above entry"
            ]

        if tp2 <= entry:
            return None, [
                "LONG TP2 must be above entry"
            ]

    else:
        if stop_loss <= entry:
            return None, [
                "SHORT stop loss must be above entry"
            ]

        if tp1 >= entry:
            return None, [
                "SHORT TP1 must be below entry"
            ]

        if tp2 >= entry:
            return None, [
                "SHORT TP2 must be below entry"
            ]

    # =========================================================
    # INDEPENDENT RR CALCULATION
    #
    # Never trust data["rr"].
    # Recalculate from Entry / SL / TP2.
    # =========================================================

    try:
        calculated_rr = calculate_rr(
            side=side,
            entry=entry,
            stop_loss=stop_loss,
            target=tp2,
        )

    except (
        TypeError,
        ValueError,
    ) as exc:
        return None, [
            f"Invalid trade levels: {exc}"
        ]

    if calculated_rr < max(
        MIN_RR,
        float(min_rr),
    ):
        return None, [
            f"Calculated RR "
            f"{calculated_rr:.2f} < required "
            f"{max(MIN_RR, float(min_rr)):.2f}"
        ]

    # =========================================================
    # TRADE PLAN
    # =========================================================

    plan = TradePlan(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        rr=calculated_rr,
    )

    # =========================================================
    # FINAL LEVEL VALIDATION
    # =========================================================

    level_ok, level_reason = validate_levels(
        plan,
        min_rr=max(
            MIN_RR,
            float(min_rr),
        ),
    )

    if not level_ok:
        return None, [
            level_reason
        ]

    # =========================================================
    # SIGNAL KEY
    #
    # One signal per symbol + side + 5M candle.
    # =========================================================

    key = make_signal_key(
        symbol,
        side,
        five,
    )

    # =========================================================
    # VALIDATED SIGNAL
    # =========================================================

    return (
        ValidatedSignal(
            key=key,
            symbol=symbol,
            side=side,
            candle_time=five,
            analysis=data,
            plan=plan,
        ),
        [],
    )
