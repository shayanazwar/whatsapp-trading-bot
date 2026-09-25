from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .risk_manager import TradePlan, validate_levels
from .setup_filter import validate_analysis


MIN_SCORE = 82
MIN_RR = 2.0
MIN_CONFIRMATION_FAMILIES = 5

FIVE_MINUTE_MS = 5 * 60 * 1000
FIFTEEN_MINUTE_MS = 15 * 60 * 1000


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


def _normalize_timestamp_ms(
    value: Any,
) -> int | None:
    try:
        timestamp = int(value)
    except (
        TypeError,
        ValueError,
    ):
        return None

    # Accept either seconds or milliseconds.
    if timestamp < 10**12:
        timestamp *= 1000

    return timestamp


def _validate_5m_timestamp(
    *,
    candle_15m_time: int,
    candle_5m_time: int,
) -> tuple[bool, str]:
    """
    Validate chronological relationship between the 15M setup
    candle and the 5M trigger.

    The 15M setup must already be closed before the 5M trigger
    can be used.

    Therefore:

        5M trigger time >= 15M setup time

    A trigger that is excessively old is rejected.
    """

    if candle_5m_time < candle_15m_time:
        return (
            False,
            "5M trigger belongs to an earlier 15M candle",
        )

    age = (
        candle_5m_time
        - candle_15m_time
    )

    # Do not allow a stale trigger from many 5M candles later.
    # The setup remains valid only for the current 15M window
    # plus one additional 5M candle of execution tolerance.
    max_age = (
        FIFTEEN_MINUTE_MS
        + FIVE_MINUTE_MS
    )

    if age > max_age:
        return (
            False,
            "5M trigger is stale relative to the 15M setup",
        )

    # 5M candles must be aligned to the normal 5-minute boundary.
    if candle_5m_time % FIVE_MINUTE_MS != 0:
        return (
            False,
            "5M trigger timestamp is not 5M-aligned",
        )

    return True, "OK"


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
    # 2. 15M CANDLE TIMESTAMP
    # ============================================================

    candle_time_raw = data.get(
        "candle_time"
    )

    if candle_time_raw is None:
        return None, [
            "15M closed candle timestamp is missing"
        ]

    candle_time = _normalize_timestamp_ms(
        candle_time_raw
    )

    if candle_time is None:
        return None, [
            "Invalid 15M candle timestamp"
        ]

    if candle_time % FIFTEEN_MINUTE_MS != 0:
        return None, [
            "15M candle timestamp is not 15M-aligned"
        ]

    # ============================================================
    # 3. 5M TRIGGER TIMESTAMP
    # ============================================================

    candle_5m_raw = data.get(
        "closed_5m_candle_time"
    )

    if candle_5m_raw is None:
        return None, [
            "5M closed candle timestamp is missing"
        ]

    candle_5m_time = _normalize_timestamp_ms(
        candle_5m_raw
    )

    if candle_5m_time is None:
        return None, [
            "Invalid 5M candle timestamp"
        ]

    timestamp_ok, timestamp_reason = (
        _validate_5m_timestamp(
            candle_15m_time=candle_time,
            candle_5m_time=candle_5m_time,
        )
    )

    if not timestamp_ok:
        return None, [
            timestamp_reason
        ]

    # ============================================================
    # 4. SIDE
    # ============================================================

    side = str(
        data.get("setup") or ""
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

    required_rr = max(
        MIN_RR,
        float(min_rr or 0),
    )

    levels_ok, level_reason = validate_levels(
        plan,
        required_rr,
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
        data.get(
            "score",
            0,
        )
        or 0
    )

    required_score = max(
        MIN_SCORE,
        int(min_confluence or 0),
    )

    if score < required_score:
        return None, [
            f"Final score {score}/100 "
            f"is below {required_score}"
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

    if family_count < MIN_CONFIRMATION_FAMILIES:
        return None, [
            f"Only {family_count}/6 "
            "confirmation families passed"
        ]

    # ============================================================
    # 11. MANDATORY FAMILY FLAGS
    # ============================================================

    if not bool(
        data.get(
            "direction_ok",
            False,
        )
    ):
        return None, [
            "Mandatory Direction family failed"
        ]

    if not bool(
        data.get(
            "structure_ok",
            False,
        )
    ):
        return None, [
            "Mandatory Structure family failed"
        ]

    if not bool(
        data.get(
            "setup_ok",
            False,
        )
    ):
        return None, [
            "Mandatory Setup family failed"
        ]

    # ============================================================
    # 12. SIDE/DIRECTION CONSISTENCY
    # ============================================================

    if side == "LONG":

        if not bool(
            data.get(
                "bullish_points",
                0,
            )
        ):
            return None, [
                "LONG signal has no bullish directional evidence"
            ]

        if bool(
            data.get(
                "bearish_points",
                0,
            )
        ):
            return None, [
                "LONG signal contains bearish directional conflict"
            ]

    else:

        if not bool(
            data.get(
                "bearish_points",
                0,
            )
        ):
            return None, [
                "SHORT signal has no bearish directional evidence"
            ]

        if bool(
            data.get(
                "bullish_points",
                0,
            )
        ):
            return None, [
                "SHORT signal contains bullish directional conflict"
            ]

    # ============================================================
    # 13. SYMBOL
    # ============================================================

    symbol = str(
        data.get("symbol") or ""
    ).upper().strip()

    if not symbol:
        return None, [
            "Signal symbol is missing"
        ]

    # ============================================================
    # 14. SIGNAL KEY
    # ============================================================

    key = make_signal_key(
        symbol,
        side,
        candle_time,
    )

    # ============================================================
    # 15. IMMUTABLE VALIDATED SIGNAL
    # ============================================================

    analysis = dict(data)

    # Store normalized timestamps so every downstream component
    # receives the same units.
    analysis[
        "candle_time"
    ] = candle_time

    analysis[
        "closed_5m_candle_time"
    ] = candle_5m_time

    analysis[
        "score"
    ] = score

    analysis[
        "confirmation_family_count"
    ] = family_count

    validated = ValidatedSignal(
        key=key,
        symbol=symbol,
        side=side,
        candle_time=candle_time,
        analysis=analysis,
        plan=plan,
    )

    return validated, []
