from __future__ import annotations

from typing import Any

MIN_SCORE = 0  # V11 score is diagnostic only
MIN_RR = 1.60
MIN_CONFIRMATION_FAMILIES = 0
MIN_AVAILABLE_CONFIRMATION_FAMILIES = 0
MIN_ATR_PERCENTILE = 0.0
MAX_ATR_PERCENTILE = 100.0
MIN_SL_ATR = 0.20
MAX_SL_ATR = 3.00
MIN_TP_ATR = 0.50


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
    if bool(data.get("score_hard_gate", False)) and score < required_score:
        reasons.append(f"Score {score} < required {required_score}")

    side = str(data.get("setup") or "").upper()
    if side not in {"LONG", "SHORT"}:
        reasons.append("Setup is not LONG or SHORT")
        return False, reasons

    hard_flags = (
        ("direction_ok", "1D macro directional boundary failed"),
        ("structure_ok", "4H continuation structure failed"),
        ("setup_ok", "4H value pullback failed"),
        ("confirmation_ok", "1H confirmation failed"),
        ("location_ok", "Higher-timeframe target path failed"),
        ("target_path_structural", "Target is not a confirmed higher-timeframe structure level"),
        ("structure_quality_ok", "4H continuation quality failed"),
        ("shock_veto_ok", "1H shock/liquidity veto failed"),
        ("btc_filter_ok", "BTC directional filter failed"),
        ("volatility_ok", "ATR volatility percentile gate failed"),
        ("confirmation_family_diversity_ok", "Confirmation-family diversity gate failed"),
        ("technical_candidate", "Engine did not mark this as a technical candidate"),
        ("trade_geometry_ok", "Single-TP structural geometry failed"),
        ("risk_ok", "Risk/RR validation failed"),
    )
    conditional_hard_flags = {
        "btc_filter_ok",
        "volatility_ok",
        "confirmation_family_diversity_ok",
    }
    for key, message in hard_flags:
        # These newer diagnostics are enforced whenever the Engine supplies them.
        # Missing fields remain backward-compatible for legacy callers/fixtures;
        # the live Engine always supplies them.
        if key in conditional_hard_flags and key not in data:
            continue
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

    atr_for_geometry = _f(data.get("atr_4h", data.get("atr")), 0.0)
    if atr_for_geometry > 0:
        try:
            entry_for_sl = float(data.get("entry"))
            stop_for_sl = float(data.get("stop_loss"))
            sl_atr = abs(entry_for_sl - stop_for_sl) / atr_for_geometry
        except (TypeError, ValueError):
            sl_atr = 999.0
    else:
        sl_atr = 999.0
    if sl_atr < MIN_SL_ATR:
        reasons.append(f"SL distance {sl_atr:.2f} ATR < minimum {MIN_SL_ATR:.2f}")
    elif sl_atr > MAX_SL_ATR:
        reasons.append(f"SL distance {sl_atr:.2f} ATR > maximum {MAX_SL_ATR:.2f}")

    if atr_for_geometry > 0:
        try:
            tp_distance_atr = abs(float(data.get("tp")) - float(data.get("entry"))) / atr_for_geometry
        except (TypeError, ValueError):
            tp_distance_atr = 0.0
    else:
        tp_distance_atr = 0.0
    if tp_distance_atr < MIN_TP_ATR:
        reasons.append(f"TP distance {tp_distance_atr:.2f} ATR < minimum {MIN_TP_ATR:.2f}")

    if "data_fresh" in data and data.get("data_fresh") is not True:
        reasons.append("Market data freshness could not be verified")
    if "futures_execution_ok" in data and data.get("futures_execution_ok") is not True:
        reasons.append("Futures execution quality could not be verified")
    if "target_path_clear" in data and data.get("target_path_clear") is not True:
        reasons.append("4H/1H target path is not clear")

    families_passed = data.get("confirmation_families_passed")
    families_available = data.get("confirmation_families_available")
    if families_passed is not None and int(_f(families_passed, 0)) < MIN_CONFIRMATION_FAMILIES:
        reasons.append(f"Confirmation families {int(_f(families_passed, 0))} < required {MIN_CONFIRMATION_FAMILIES}")
    if families_available is not None and int(_f(families_available, 0)) < MIN_AVAILABLE_CONFIRMATION_FAMILIES:
        reasons.append(f"Available confirmation families {int(_f(families_available, 0))} < required {MIN_AVAILABLE_CONFIRMATION_FAMILIES}")

    atr_percentile = data.get("atr_percentile")
    if atr_percentile is not None:
        atr_rank = _f(atr_percentile, -1.0)
        if not (MIN_ATR_PERCENTILE <= atr_rank <= MAX_ATR_PERCENTILE):
            reasons.append(f"ATR percentile {atr_rank:.1f} outside {MIN_ATR_PERCENTILE:.1f}-{MAX_ATR_PERCENTILE:.1f}")

    if require_increasing_volume and str(data.get("volume_status") or data.get("volume") or "").upper() != "INCREASING":
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
