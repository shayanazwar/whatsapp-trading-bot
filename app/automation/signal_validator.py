from __future__ import annotations

import hashlib
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


def make_signal_key(
    symbol: str,
    side: str,
    candle_time: int,
) -> str:

    raw = (
        f"mexc|"
        f"{symbol.upper()}|"
        f"{side.upper()}|"
        f"{int(candle_time)}"
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:40]


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

    # ============================================================
    # 1. COMPLETE ANALYSIS VALIDATION
    # ============================================================

    valid, reasons = validate_analysis(
        data,
        min_confluence=min_confluence,
        min_rr=min_rr,
        require_increasing_volume=(
            require_increasing_volume
        ),
    )

    if not valid:
        return None, reasons

    # ============================================================
    # 2. CANDLE TIMESTAMP
    # ============================================================

    candle_time = data.get(
        "candle_time"
    )

    if candle_time is None:
        return None, [
            "15M closed candle timestamp is missing"
        ]

    try:
        candle_time = int(
            candle_time
        )
    except (
        TypeError,
        ValueError,
    ):
        return None, [
            "Invalid 15M candle timestamp"
        ]

    # ============================================================
    # 3. 5M TIMESTAMP
    # ============================================================

    candle_5m_time = data.get(
        "closed_5m_candle_time"
    )

    if candle_5m_time is None:
        return None, [
            "5M closed candle timestamp is missing"
        ]

    try:
        candle_5m_time = int(
            candle_5m_time
        )
    except (
        TypeError,
        ValueError,
    ):
        return None, [
            "Invalid 5M candle timestamp"
        ]

    # 5M trigger must not belong to a future candle.
    if candle_5m_time > candle_time:
        return None, [
            "5M trigger timestamp is ahead of 15M analysis"
        ]

    # ============================================================
    # 4. SIDE
    # ============================================================

    side = str(
        data.get("setup")
    ).upper()

    if side not in {
        "LONG",
        "SHORT",
    }:
        return None, [
            "Invalid deterministic trade side"
        ]

    # ============================================================
    # 5. TRADE LEVELS
    # ============================================================

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

        rr = float(
            data["rr"]
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ):
        return None, [
            "Invalid trade plan values"
        ]

    if (
        entry <= 0
        or stop_loss <= 0
        or tp1 <= 0
        or tp2 <= 0
        or rr <= 0
    ):
        return None, [
            "Trade plan contains non-positive values"
        ]

    # ============================================================
    # 6. TRADE PLAN
    # ============================================================

    plan = TradePlan(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        rr=rr,
    )

    # ============================================================
    # 7. RISK MANAGER VALIDATION
    # ============================================================

    levels_ok, level_reason = validate_levels(
        plan,
        max(
            2.0,
            float(min_rr or 0),
        ),
    )

    if not levels_ok:
        return None, [
            level_reason
        ]

    # ============================================================
    # 8. 5M TRIGGER CONSISTENCY
    # ============================================================

    five_minute_long = bool(
        data.get(
            "five_minute_long",
            False,
        )
    )

    five_minute_short = bool(
        data.get(
            "five_minute_short",
            False,
        )
    )

    if side == "LONG":

        if not five_minute_long:
            return None, [
                "LONG signal has no valid 5M trigger"
            ]

        if five_minute_short:
            return None, [
                "LONG signal contains conflicting 5M SHORT trigger"
            ]

    else:

        if not five_minute_short:
            return None, [
                "SHORT signal has no valid 5M trigger"
            ]

        if five_minute_long:
            return None, [
                "SHORT signal contains conflicting 5M LONG trigger"
            ]

    # ============================================================
    # 9. SCORE CONSISTENCY
    # ============================================================

    score = int(
        data.get("score", 0)
        or 0
    )

    if score < 82:
        return None, [
            f"Final score {score}/100 is below 82"
        ]

    # ============================================================
    # 10. CONFIRMATION FAMILIES
    # ============================================================

    family_count = int(
        data.get(
            "confirmation_family_count",
            0,
        )
        or 0
    )

    if family_count < 5:
        return None, [
            f"Only {family_count}/6 confirmation families passed"
        ]

    # ============================================================
    # 11. SIGNAL KEY
    # ============================================================

    symbol = str(
        data.get("symbol")
    ).upper()

    if not symbol:
        return None, [
            "Signal symbol is missing"
        ]

    key = make_signal_key(
        symbol,
        side,
        candle_time,
    )

    # ============================================================
    # 12. IMMUTABLE VALIDATED SIGNAL
    # ============================================================

    validated = ValidatedSignal(
        key=key,
        symbol=symbol,
        side=side,
        candle_time=candle_time,
        analysis=dict(data),
        plan=plan,
    )

    return validated, []
