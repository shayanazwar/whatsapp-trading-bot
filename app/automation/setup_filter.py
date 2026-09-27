from __future__ import annotations

from typing import Any


MIN_SCORE = 82
MIN_RR = 2.0

MIN_SL_ATR = 0.50
MAX_SL_ATR = 1.80

MIN_CONFIRMATION_FAMILIES = 5

MAX_ATR_PERCENTILE = 95.0
MIN_ATR_PERCENTILE = 20.0


def _f(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        return (
            float(value)
            if value is not None
            else default
        )
    except (
        TypeError,
        ValueError,
    ):
        return default


def validate_analysis(
    data: dict[str, Any],
    *,
    min_confluence: int,
    min_rr: float,
    require_increasing_volume: bool = False,
) -> tuple[bool, list[str]]:
    """
    Final setup filter.

    This layer validates the engine's already-produced analysis.
    It must NOT create a new LONG/SHORT setup.

    The engine remains responsible for:
        4H regime
        1H alignment
        15M structure/setup
        5M trigger
        momentum
        volume
        location
        volatility
        trade levels

    This filter only performs the final consistency checks.
    """

    reasons: list[str] = []

    # =========================================================
    # BASIC SETUP
    # =========================================================

    side = str(
        data.get("setup")
        or ""
    ).upper()

    if side not in {
        "LONG",
        "SHORT",
    }:
        reasons.append(
            "No deterministic LONG/SHORT setup"
        )

    # =========================================================
    # SCORE
    # =========================================================

    score = int(
        _f(
            data.get("score"),
            0,
        )
    )

    required_score = max(
        MIN_SCORE,
        int(
            min_confluence or 0
        ),
    )

    if score < required_score:
        reasons.append(
            f"Score {score}/100 < required {required_score}"
        )

    # =========================================================
    # RISK / REWARD
    # =========================================================

    rr = _f(
        data.get("rr"),
        0.0,
    )

    required_rr = max(
        MIN_RR,
        float(
            min_rr or 0
        ),
    )

    if rr < required_rr:
        reasons.append(
            f"RR {rr:.2f} < required {required_rr:.2f}"
        )

    # =========================================================
    # CORE ENGINE GATES
    #
    # These are the gates that define whether the engine
    # considers the setup technically valid.
    #
    # Do NOT add duplicate RSI/RVOL/5M gates below.
    # =========================================================

    hard_gates = (
        (
            "direction_ok",
            "4H/1H directional alignment failed",
        ),
        (
            "structure_ok",
            "Market structure confirmation failed",
        ),
        (
            "setup_ok",
            "15M setup / 5M trigger failed",
        ),
        (
            "location_ok",
            "Location / structural target path failed",
        ),
        (
            "volatility_ok",
            "Volatility hard gate failed",
        ),
        (
            "btc_filter_ok",
            "BTC/global filter failed",
        ),
        (
            "data_fresh",
            "Market data is stale",
        ),
        (
            "target_path_structural",
            "Targets are not backed by structural/liquidity levels",
        ),
    )

    for key, message in hard_gates:
        if not bool(
            data.get(
                key,
                False,
            )
        ):
            reasons.append(message)

    # =========================================================
    # ENGINE TECHNICAL CANDIDATE
    #
    # If the engine explicitly says this is not a technical
    # candidate, do not allow this layer to resurrect it.
    # =========================================================

    if not bool(
        data.get(
            "technical_candidate",
            False,
        )
    ):
        reasons.append(
            "Engine did not mark this as a technical candidate"
        )

    # =========================================================
    # ENGINE BLOCK
    # =========================================================

    if bool(
        data.get(
            "signal_blocked",
            False,
        )
    ):
        reasons.append(
            "Engine marked signal blocked"
        )

    # =========================================================
    # CONFIRMATION FAMILIES
    #
    # Six families:
    #
    # 1. Direction
    # 2. Structure
    # 3. Setup
    # 4. Momentum
    # 5. Volume
    # 6. Location
    #
    # Minimum = 5/6.
    #
    # Futures context is intentionally NOT included because
    # it is a supporting 5-point context family in the scanner.
    # =========================================================

    families = int(
        _f(
            data.get(
                "confirmation_family_count",
            ),
            0,
        )
    )

    if families < MIN_CONFIRMATION_FAMILIES:
        reasons.append(
            f"Confirmation families "
            f"{families}/6 < required "
            f"{MIN_CONFIRMATION_FAMILIES}/6"
        )

    # =========================================================
    # 5M TRIGGER CONSISTENCY
    #
    # The engine already determines the trigger.
    # We only verify that its reported state is internally
    # consistent with the selected side.
    # =========================================================

    trigger_ready = bool(
        data.get(
            "five_minute_ready",
            False,
        )
    )

    trigger_long = bool(
        data.get(
            "five_minute_long",
            False,
        )
    )

    trigger_short = bool(
        data.get(
            "five_minute_short",
            False,
        )
    )

    if side in {
        "LONG",
        "SHORT",
    }:
        if not trigger_ready:
            reasons.append(
                "5M trigger is not ready"
            )

        if side == "LONG" and not trigger_long:
            reasons.append(
                "5M LONG trigger is not confirmed"
            )

        if side == "SHORT" and not trigger_short:
            reasons.append(
                "5M SHORT trigger is not confirmed"
            )

    if trigger_long and trigger_short:
        reasons.append(
            "Conflicting 5M triggers"
        )

    # =========================================================
    # TRIGGER QUALITY
    #
    # This remains a consistency check, but we do not create
    # another separate RSI/RVOL hard gate here.
    #
    # The engine's setup/score logic already incorporates
    # trigger quality.
    # =========================================================

    trigger_q = _f(
        data.get(
            "trigger_quality_5m",
        ),
        0.0,
    )

    if trigger_q < 0.55:
        reasons.append(
            f"5M trigger quality "
            f"{trigger_q:.2f} < 0.55"
        )

    # =========================================================
    # VOLUME REQUIREMENT
    #
    # Only enforce increasing volume if explicitly enabled
    # by configuration.
    #
    # Normal RVOL remains part of the engine's volume family
    # rather than becoming a second hidden hard gate.
    # =========================================================

    if (
        require_increasing_volume
        and str(
            data.get("volume")
            or ""
        ).upper()
        != "INCREASING"
    ):
        reasons.append(
            "15M volume is not INCREASING"
        )

    # =========================================================
    # SL / ATR
    # =========================================================

    sl_atr = _f(
        data.get(
            "sl_atr",
        ),
        999.0,
    )

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

    # =========================================================
    # ATR VOLATILITY PERCENTILE
    # =========================================================

    atr_rank = _f(
        data.get(
            "atr_percentile",
        ),
        50.0,
    )

    if (
        atr_rank < MIN_ATR_PERCENTILE
        or atr_rank > MAX_ATR_PERCENTILE
    ):
        reasons.append(
            "ATR volatility percentile "
            "outside allowed range"
        )

    # =========================================================
    # MEXC EXECUTION QUALITY
    #
    # These values may be unavailable before live-context
    # enrichment. Defaults therefore remain permissive.
    # =========================================================

    spread = _f(
        data.get(
            "mexc_spread_pct",
        ),
        0.0,
    )

    max_spread = _f(
        data.get(
            "max_mexc_spread_pct",
        ),
        0.001,
    )

    if spread > max_spread:
        reasons.append(
            "MEXC spread gate failed"
        )

    drift = _f(
        data.get(
            "entry_drift_pct",
        ),
        0.0,
    )

    max_drift = _f(
        data.get(
            "max_entry_drift_pct",
        ),
        0.002,
    )

    if drift > max_drift:
        reasons.append(
            "Entry drift gate failed"
        )

    # =========================================================
    # TRADE LEVELS
    # =========================================================

    entry = data.get("entry")
    stop_loss = data.get("stop_loss")
    tp1 = data.get("tp1")
    tp2 = data.get("tp2")

    if (
        entry is None
        or stop_loss is None
        or tp1 is None
        or tp2 is None
    ):
        reasons.append(
            "Trade levels are incomplete"
        )
    else:
        if (
            _f(entry, 0.0) <= 0
            or _f(stop_loss, 0.0) <= 0
            or _f(tp1, 0.0) <= 0
            or _f(tp2, 0.0) <= 0
        ):
            reasons.append(
                "Trade levels contain invalid values"
            )

    # =========================================================
    # FUTURES CONTEXT
    #
    # futures_ok is deliberately NOT an independent hard gate.
    #
    # Missing futures flow must not erase an otherwise valid
    # technical setup.
    #
    # It contributes supporting context/score through the
    # scanner.
    # =========================================================

    # No direct rejection for futures_ok=False.

    # =========================================================
    # FINAL RESULT
    # =========================================================

    return (
        len(reasons) == 0,
        reasons,
    )
