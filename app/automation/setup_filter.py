from __future__ import annotations

from typing import Any

# The structural engine is authoritative. These thresholds are safety floors,
# not a substitute for structure or a fixed-percentage signal rule.
MIN_SCORE = 72
MIN_RR = 2.00
MIN_CONFIRMATION_FAMILIES = 0  # legacy compatibility; intentionally non-gating
MIN_AVAILABLE_CONFIRMATION_FAMILIES = 0  # legacy compatibility; intentionally non-gating
MIN_SL_ATR = 1.00
MAX_SL_ATR = 3.50
MIN_TP_ATR = 2.50


def _f(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if value == value else default
    except (TypeError, ValueError):
        return default


def validate_analysis(
    data: dict[str, Any],
    *,
    min_confluence: int = MIN_SCORE,
    min_rr: float = MIN_RR,
    require_increasing_volume: bool = False,
) -> tuple[bool, list[str]]:
    """Validate the final structural signal without a lower-timeframe/family gate."""
    reasons: list[str] = []
    score = int(_f(data.get("score"), 0))
    required_score = max(MIN_SCORE, int(min_confluence))
    if score < required_score:
        reasons.append(f"Score {score} < required {required_score}")

    side = str(data.get("setup") or "").upper()
    if side not in {"LONG", "SHORT"}:
        reasons.append("Setup is not LONG or SHORT")
        return False, reasons

    # Core structural gates remain hard because they define what the signal means.
    hard_flags = (
        ("direction_ok", "4H/1H directional structure failed"),
        ("structure_ok", "15M BOS/retest structure failed"),
        ("setup_ok", "15M setup failed"),
        ("location_ok", "Structural target path failed"),
        ("target_path_structural", "Target is not a confirmed higher-timeframe structure level"),
        ("structure_quality_ok", "BOS/retest quality failed"),
        ("shock_veto_ok", "Shock/liquidity veto failed"),
        ("technical_candidate", "Engine did not mark this as a technical candidate"),
    )
    for key, message in hard_flags:
        if data.get(key) is not True:
            reasons.append(message)

    primary_tf = str(data.get("primary_entry_timeframe") or "15M").upper()
    if primary_tf != "15M":
        reasons.append("Primary entry timeframe must be 15M")

    rr = _f(data.get("rr"), 0.0)
    required_rr = max(MIN_RR, float(min_rr or 0.0))
    if rr + 1e-12 < required_rr:
        reasons.append(f"RR {rr:.2f} < required {required_rr:.2f}")

    # Structural ATR geometry is required; no exact price-distance percentages are used.
    sl_atr = _f(data.get("sl_atr"), 999.0)
    if sl_atr < MIN_SL_ATR:
        reasons.append(f"SL distance {sl_atr:.2f} ATR < minimum {MIN_SL_ATR:.2f}")
    elif sl_atr > MAX_SL_ATR:
        reasons.append(f"SL distance {sl_atr:.2f} ATR > maximum {MAX_SL_ATR:.2f}")

    tp_distance_atr = _f(data.get("tp_distance_atr"), 0.0)
    if tp_distance_atr < MIN_TP_ATR:
        reasons.append(f"TP distance {tp_distance_atr:.2f} ATR < minimum {MIN_TP_ATR:.2f}")

    if data.get("trade_geometry_ok") is not True:
        reasons.append("Single-TP structural geometry failed")

    spread = _f(data.get("mexc_spread_pct"), 0.0)
    max_spread = _f(data.get("max_mexc_spread_pct"), 0.001)
    if spread > max_spread:
        reasons.append("MEXC execution spread safety gate failed")

    try:
        entry = float(data.get("entry"))
        stop_loss = float(data.get("stop_loss"))
        tp = float(data.get("tp"))
    except (TypeError, ValueError):
        entry = stop_loss = tp = 0.0
    if not (entry > 0 and stop_loss > 0 and tp > 0):
        reasons.append("Single trade levels are incomplete or invalid")
    elif side == "LONG" and not (stop_loss < entry < tp):
        reasons.append("LONG levels must satisfy SL < Entry < TP")
    elif side == "SHORT" and not (tp < entry < stop_loss):
        reasons.append("SHORT levels must satisfy TP < Entry < SL")

    # Volume direction is supporting evidence only. Keep the legacy argument for
    # API compatibility, but never let it override a valid structural setup.

    return len(reasons) == 0, list(dict.fromkeys(reasons))
