from __future__ import annotations

from typing import Any


# ============================================================
# GOLD STANDARD V1 HARD GATES
# ============================================================

MIN_SCORE = 82
MIN_RR = 2.0

MIN_SL_ATR = 0.50
MAX_SL_ATR = 1.80

MIN_CONFIRMATION_FAMILIES = 5


def validate_analysis(
    data: dict[str, Any],
    *,
    min_confluence: int,
    min_rr: float,
    require_increasing_volume: bool = False,
) -> tuple[bool, list[str]]:

    reasons: list[str] = []

    side = str(
        data.get("setup") or ""
    ).upper()

    # ========================================================
    # 1. DETERMINISTIC SIDE
    # ========================================================

    if side not in {
        "LONG",
        "SHORT",
    }:
        return False, [
            "No deterministic LONG/SHORT setup"
        ]

    # ========================================================
    # 2. SCORE
    # ========================================================

    score = int(
        data.get("score", 0) or 0
    )

    required_score = max(
        MIN_SCORE,
        int(min_confluence or 0),
    )

    if score < required_score:
        reasons.append(
            f"Score {score}/100 < required {required_score}"
        )

    # ========================================================
    # 3. RISK / REWARD
    # ========================================================

    try:
        rr = float(
            data.get("rr") or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        rr = 0.0

    required_rr = max(
        MIN_RR,
        float(min_rr or 0),
    )

    if rr < required_rr:
        reasons.append(
            f"RR {rr:.2f} < required {required_rr:.2f}"
        )

    # ========================================================
    # 4. DIRECTION
    # ========================================================

    direction_ok = bool(
        data.get(
            "direction_ok",
            False,
        )
    )

    if not direction_ok:
        reasons.append(
            "4H/1H directional alignment failed"
        )

    # ========================================================
    # 5. MARKET STRUCTURE
    # ========================================================

    structure_ok = bool(
        data.get(
            "structure_ok",
            False,
        )
    )

    if not structure_ok:
        reasons.append(
            "Market structure confirmation failed"
        )

    # ========================================================
    # 6. 15M SETUP + 5M TRIGGER
    # ========================================================

    setup_ok = bool(
        data.get(
            "setup_ok",
            False,
        )
    )

    if not setup_ok:
        reasons.append(
            "15M setup / 5M trigger failed"
        )

    five_minute_ready = bool(
        data.get(
            "five_minute_ready",
            False,
        )
    )

    if not five_minute_ready:
        reasons.append(
            "5M data is not ready"
        )

    five_long = bool(
        data.get(
            "five_minute_long",
            False,
        )
    )

    five_short = bool(
        data.get(
            "five_minute_short",
            False,
        )
    )

    if side == "LONG":

        if not five_long:
            reasons.append(
                "5M LONG trigger confirmation failed"
            )

        if five_short:
            reasons.append(
                "LONG contains conflicting 5M SHORT trigger"
            )

    else:

        if not five_short:
            reasons.append(
                "5M SHORT trigger confirmation failed"
            )

        if five_long:
            reasons.append(
                "SHORT contains conflicting 5M LONG trigger"
            )

    # ========================================================
    # 7. MOMENTUM
    # ========================================================

    momentum_ok = bool(
        data.get(
            "momentum_ok",
            False,
        )
    )

    if not momentum_ok:
        reasons.append(
            "Momentum confirmation failed"
        )

    # ========================================================
    # 8. VOLUME / PARTICIPATION
    # ========================================================

    volume_ok = bool(
        data.get(
            "volume_ok",
            False,
        )
    )

    if not volume_ok:
        reasons.append(
            "Multi-timeframe volume confirmation failed"
        )

    if (
        require_increasing_volume
        and str(
            data.get("volume") or ""
        ).upper()
        != "INCREASING"
    ):
        reasons.append(
            "15M volume is not INCREASING"
        )

    # ========================================================
    # 9. LOCATION / TARGET PATH
    # ========================================================

    location_ok = bool(
        data.get(
            "location_ok",
            False,
        )
    )

    if not location_ok:
        reasons.append(
            "Location / target path failed"
        )

    # ========================================================
    # 10. FUTURES CONTEXT
    # ========================================================

    futures_ok = bool(
        data.get(
            "futures_ok",
            False,
        )
    )

    if not futures_ok:
        reasons.append(
            "Futures market context failed"
        )

    # ========================================================
    # 11. VOLATILITY / EXECUTION
    # ========================================================

    volatility_ok = bool(
        data.get(
            "volatility_ok",
            False,
        )
    )

    if not volatility_ok:
        reasons.append(
            "Volatility / execution conditions failed"
        )

    # ========================================================
    # 12. CONFIRMATION FAMILIES
    #
    # Direction + Structure + Setup are mandatory.
    # At least 5 of 6 families must pass.
    #
    # Futures and volatility are separate hard gates and are
    # intentionally NOT counted as confirmation families.
    # ========================================================

    families = [
        direction_ok,
        structure_ok,
        setup_ok,
        momentum_ok,
        volume_ok,
        location_ok,
    ]

    family_count = sum(
        1
        for value in families
        if value
    )

    data[
        "confirmation_family_count"
    ] = family_count

    if family_count < MIN_CONFIRMATION_FAMILIES:
        reasons.append(
            "Confirmation families "
            f"{family_count}/6 < required "
            f"{MIN_CONFIRMATION_FAMILIES}/6"
        )

    if not direction_ok:
        reasons.append(
            "Mandatory Direction family missing"
        )

    if not structure_ok:
        reasons.append(
            "Mandatory Structure family missing"
        )

    if not setup_ok:
        reasons.append(
            "Mandatory Setup family missing"
        )

    # ========================================================
    # 13. BULLISH / BEARISH COMPATIBILITY
    # ========================================================

    try:
        bullish = int(
            data.get(
                "bullish_points",
                0,
            )
            or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        bullish = 0

    try:
        bearish = int(
            data.get(
                "bearish_points",
                0,
            )
            or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        bearish = 0

    if side == "LONG":

        if bullish < 4:
            reasons.append(
                "LONG directional factors "
                "are not fully aligned"
            )

        if bearish != 0:
            reasons.append(
                "LONG has conflicting bearish factors"
            )

    else:

        if bearish < 4:
            reasons.append(
                "SHORT directional factors "
                "are not fully aligned"
            )

        if bullish != 0:
            reasons.append(
                "SHORT has conflicting bullish factors"
            )

    # ========================================================
    # 14. PRICE LEVELS
    # ========================================================

    try:
        entry = float(
            data.get("entry") or 0
        )

        sl = float(
            data.get("stop_loss") or 0
        )

        tp1 = float(
            data.get("tp1") or 0
        )

        tp2 = float(
            data.get("tp2") or 0
        )

    except (
        TypeError,
        ValueError,
    ):
        reasons.append(
            "Invalid trade levels"
        )

        return False, reasons

    if (
        entry <= 0
        or sl <= 0
        or tp1 <= 0
        or tp2 <= 0
    ):
        reasons.append(
            "Trade levels contain invalid values"
        )

    # ========================================================
    # 15. PRICE ORDER
    # ========================================================

    if side == "LONG":

        if not (
            sl < entry < tp1 < tp2
        ):
            reasons.append(
                "LONG price ordering failed"
            )

    else:

        if not (
            tp2 < tp1 < entry < sl
        ):
            reasons.append(
                "SHORT price ordering failed"
            )

    # ========================================================
    # 16. SUPPORT / RESISTANCE
    # ========================================================

    support = data.get(
        "support"
    )

    resistance = data.get(
        "resistance"
    )

    try:

        if (
            side == "LONG"
            and resistance is not None
            and entry >= float(resistance)
        ):
            reasons.append(
                "LONG entry is at/above detected resistance"
            )

        if (
            side == "SHORT"
            and support is not None
            and entry <= float(support)
        ):
            reasons.append(
                "SHORT entry is at/below detected support"
            )

    except (
        TypeError,
        ValueError,
    ):
        reasons.append(
            "Invalid support/resistance data"
        )

    # ========================================================
    # 17. ATR STOP DISTANCE
    # ========================================================

    try:
        atr_value = float(
            data.get("atr") or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        atr_value = 0.0

    if atr_value <= 0:
        reasons.append(
            "ATR is invalid"
        )

    else:

        sl_distance = abs(
            entry - sl
        )

        sl_atr = (
            sl_distance
            / atr_value
        )

        data[
            "sl_atr"
        ] = sl_atr

        if sl_atr < MIN_SL_ATR:
            reasons.append(
                f"SL distance {sl_atr:.2f} ATR "
                f"< minimum {MIN_SL_ATR:.2f}"
            )

        if sl_atr > MAX_SL_ATR:
            reasons.append(
                f"SL distance {sl_atr:.2f} ATR "
                f"> maximum {MAX_SL_ATR:.2f}"
            )

    # ========================================================
    # 18. 5M TRIGGER QUALITY
    # ========================================================

    try:
        trigger_quality = float(
            data.get(
                "trigger_quality_5m",
                0,
            )
            or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        trigger_quality = 0.0

    if trigger_quality < 0.55:
        reasons.append(
            f"5M trigger quality "
            f"{trigger_quality:.2f} < 0.55"
        )

    # ========================================================
    # 19. RSI SANITY
    # ========================================================

    try:
        rsi_15m = float(
            data.get(
                "rsi",
                50,
            )
            or 50
        )
    except (
        TypeError,
        ValueError,
    ):
        rsi_15m = 50.0

    try:
        rsi_5m = float(
            data.get(
                "rsi_5m",
                50,
            )
            or 50
        )
    except (
        TypeError,
        ValueError,
    ):
        rsi_5m = 50.0

    if side == "LONG":

        if rsi_15m <= 50:
            reasons.append(
                "15M RSI does not confirm LONG"
            )

        if rsi_5m <= 50:
            reasons.append(
                "5M RSI does not confirm LONG"
            )

        if rsi_5m >= 75:
            reasons.append(
                "5M RSI is excessively extended"
            )

    else:

        if rsi_15m >= 50:
            reasons.append(
                "15M RSI does not confirm SHORT"
            )

        if rsi_5m >= 50:
            reasons.append(
                "5M RSI does not confirm SHORT"
            )

        if rsi_5m <= 25:
            reasons.append(
                "5M RSI is excessively extended"
            )

    # ========================================================
    # 20. RVOL SANITY
    # ========================================================

    try:
        rvol_15m = float(
            data.get(
                "rvol",
                0,
            )
            or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        rvol_15m = 0.0

    try:
        rvol_5m = float(
            data.get(
                "rvol_5m",
                0,
            )
            or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        rvol_5m = 0.0

    if rvol_15m < 1.0:
        reasons.append(
            "15M RVOL < 1.0"
        )

    if rvol_5m < 1.0:
        reasons.append(
            "5M RVOL < 1.0"
        )

    # ========================================================
    # 21. FINAL HARD GATES
    # ========================================================

    if not futures_ok:
        reasons.append(
            "Futures context hard gate failed"
        )

    if not volatility_ok:
        reasons.append(
            "Volatility hard gate failed"
        )

    # ========================================================
    # 22. RESULT
    # ========================================================

    return (
        len(reasons) == 0,
        reasons,
    )
