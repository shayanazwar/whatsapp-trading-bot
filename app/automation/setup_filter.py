from __future__ import annotations

from typing import Any

MIN_SCORE = 65
MIN_RR = 2.00
MIN_CONFIRMATION_FAMILIES = 0
MIN_AVAILABLE_CONFIRMATION_FAMILIES = 0
MIN_SL_ATR = 0.50
MAX_SL_ATR = 1.25
MIN_TP_ATR = 1.00


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
    """Validate the final 1D/12H/4H/1H structural signal."""
    reasons: list[str] = []
    score = int(_f(data.get("score"), 0))
    required_score = max(MIN_SCORE, int(min_confluence))
    if score < required_score:
        reasons.append(f"Score {score} < required {required_score}")

    side = str(data.get("setup") or "").upper()
    if side not in {"LONG", "SHORT"}:
        reasons.append("Setup is not LONG or SHORT")
        return False, reasons

    hard_flags = (
        ("direction_ok", "1D/12H/4H directional alignment failed"),
        ("structure_ok", "4H BOS/retest structure failed"),
        ("setup_ok", "1H setup failed"),
        ("confirmation_ok", "1H confirmation failed"),
        ("location_ok", "Higher-timeframe target path failed"),
        ("target_path_structural", "Target is not a confirmed higher-timeframe structure level"),
        ("structure_quality_ok", "4H BOS/retest quality failed"),
        ("shock_veto_ok", "1H shock/liquidity veto failed"),
        ("technical_candidate", "Engine did not mark this as a technical candidate"),
        ("trade_geometry_ok", "Single-TP structural geometry failed"),
        ("risk_ok", "Risk/RR validation failed"),
    )
    for key, message in hard_flags:
        if data.get(key) is not True:
            reasons.append(message)

    primary_tf = str(data.get("primary_entry_timeframe") or "").upper()
    if primary_tf != "1H":
        reasons.append("Primary entry timeframe must be 1H")

    signal_tf = str(data.get("signal_candle_timeframe") or "1H").upper()
    if signal_tf != "1H":
        reasons.append("Signal candle timeframe must be 1H")

    rr = _f(data.get("rr"), 0.0)
    required_rr = max(MIN_RR, float(min_rr or 0.0))
    if rr + 1e-12 < required_rr:
        reasons.append(f"RR {rr:.2f} < required {required_rr:.2f}")

    sl_atr = _f(data.get("sl_atr"), 999.0)
    if sl_atr < MIN_SL_ATR:
        reasons.append(f"SL distance {sl_atr:.2f} ATR < minimum {MIN_SL_ATR:.2f}")
    elif sl_atr > MAX_SL_ATR:
        reasons.append(f"SL distance {sl_atr:.2f} ATR > maximum {MAX_SL_ATR:.2f}")

    tp_distance_atr = _f(data.get("tp_distance_atr"), 0.0)
    if tp_distance_atr < MIN_TP_ATR:
        reasons.append(f"TP distance {tp_distance_atr:.2f} ATR < minimum {MIN_TP_ATR:.2f}")

    if require_increasing_volume and str(data.get("volume_status") or "").upper() != "INCREASING":
        reasons.append("Volume is not increasing")

    spread = _f(data.get("mexc_spread_pct"), 0.0)
    max_spread = _f(data.get("max_allowed_spread_pct"), 0.50)
    if spread > max_spread:
        reasons.append(f"MEXC spread {spread:.3f}% > {max_spread:.3f}%")

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

    return len(reasons) == 0, list(dict.fromkeys(reasons))
