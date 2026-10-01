from __future__ import annotations

from typing import Any


MIN_SCORE = 78
MIN_RR = 2.50
MIN_CONFIRMATION_FAMILIES = 5
MIN_AVAILABLE_CONFIRMATION_FAMILIES = 6
MIN_SL_ATR = 1.00
MAX_SL_ATR = 3.50
MIN_TP_ATR = 2.50


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def validate_analysis(
    data: dict[str, Any],
    *,
    min_confluence: int,
    min_rr: float,
    require_increasing_volume: bool = False,
) -> tuple[bool, list[str]]:
    """Final consistency/safety validator.

    Structural prerequisites remain hard: 4H/1H/15M BOS+retest, clean target
    geometry, BTC/data quality, and a valid single-TP trade plan. Supporting
    evidence uses the 5-of-8 family model. Missing optional families abstain.
    """
    reasons: list[str] = []
    side = str(data.get("setup") or "").upper()

    if side not in {"LONG", "SHORT"}:
        reasons.append("No deterministic LONG/SHORT setup")

    score = int(_f(data.get("score"), 0))
    required_score = max(MIN_SCORE, int(min_confluence or 0))
    if score < required_score:
        reasons.append(f"Score {score}/100 < required {required_score}")

    rr = _f(data.get("rr"), 0.0)
    required_rr = max(MIN_RR, float(min_rr or 0.0))
    if rr < required_rr:
        reasons.append(f"RR {rr:.2f} < required {required_rr:.2f}")

    # Structural prerequisites; these are intentionally not part of the 5/8 vote.
    for key, message in (
        ("direction_ok", "4H/1H directional alignment failed"),
        ("structure_ok", "Market structure confirmation failed"),
        ("setup_ok", "15M BOS + retest setup failed"),
        ("btc_filter_ok", "BTC/global filter failed"),
        ("data_fresh", "Market data is stale"),
        ("target_path_structural", "TP is not backed by a structural HTF target"),
        ("structure_quality_ok", "BOS/retest quality failed"),
        ("shock_veto_ok", "Shock/liquidity veto failed"),
        ("technical_candidate", "Engine did not mark this as a technical candidate"),
    ):
        if not bool(data.get(key, False)):
            reasons.append(message)

    if bool(data.get("signal_blocked", False)):
        reasons.append("Engine marked signal blocked")

    family_data = data.get("confirmation_families")
    family_passed = data.get("confirmation_families_passed")
    family_available = data.get("confirmation_families_available")
    diversity_ok = data.get("confirmation_family_diversity_ok")

    if isinstance(family_data, dict):
        statuses = [
            str(value.get("status") or "ABSTAIN").upper()
            for value in family_data.values()
            if isinstance(value, dict)
        ]
        derived_passed = sum(status == "PASS" for status in statuses)
        derived_available = sum(status in {"PASS", "FAIL"} for status in statuses)
        if family_passed is None:
            family_passed = derived_passed
        if family_available is None:
            family_available = derived_available

    passed = int(_f(family_passed, 0))
    available = int(_f(family_available, 0))
    if passed < MIN_CONFIRMATION_FAMILIES or available < MIN_AVAILABLE_CONFIRMATION_FAMILIES:
        reasons.append(
            f"Confirmation families {passed}/{available} < required "
            f"{MIN_CONFIRMATION_FAMILIES}/{MIN_AVAILABLE_CONFIRMATION_FAMILIES}"
        )
    if diversity_ok is False or (family_data is not None and diversity_ok is None):
        reasons.append("Confirmation family diversity requirement failed")

    primary_tf = str(data.get("primary_entry_timeframe") or "15M").upper()
    if side in {"LONG", "SHORT"} and primary_tf != "15M":
        reasons.append("Primary entry timeframe must be 15M")

    sl_atr = _f(data.get("sl_atr"), 999.0)
    if sl_atr < MIN_SL_ATR:
        reasons.append(f"SL distance {sl_atr:.2f} ATR < minimum {MIN_SL_ATR:.2f}")
    if sl_atr > MAX_SL_ATR:
        reasons.append(f"SL distance {sl_atr:.2f} ATR > maximum {MAX_SL_ATR:.2f}")

    if side in {"LONG", "SHORT"} and data.get("trade_geometry_ok") is False:
        reasons.append("Single-TP trade geometry gate failed")

    tp_distance_atr = _f(data.get("tp_distance_atr"), 0.0)
    if side in {"LONG", "SHORT"} and tp_distance_atr < MIN_TP_ATR:
        reasons.append(f"TP distance {tp_distance_atr:.2f} ATR < minimum {MIN_TP_ATR:.2f}")

    spread = _f(data.get("mexc_spread_pct"), 0.0)
    max_spread = _f(data.get("max_mexc_spread_pct"), 0.001)
    if spread > max_spread:
        reasons.append("MEXC spread gate failed")

    entry = _f(data.get("entry"), 0.0)
    stop_loss = _f(data.get("stop_loss"), 0.0)
    tp = _f(data.get("tp"), 0.0)
    if entry <= 0 or stop_loss <= 0 or tp <= 0:
        reasons.append("Single trade levels are incomplete or invalid")
    elif side == "LONG" and not (stop_loss < entry < tp):
        reasons.append("LONG levels must satisfy SL < Entry < TP")
    elif side == "SHORT" and not (tp < entry < stop_loss):
        reasons.append("SHORT levels must satisfy TP < Entry < SL")

    # Increasing volume is deliberately supporting evidence only.
    _ = require_increasing_volume
    return len(reasons) == 0, list(dict.fromkeys(reasons))
