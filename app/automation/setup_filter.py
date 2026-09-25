from __future__ import annotations

from typing import Any

MIN_SCORE = 82
MIN_RR = 2.0
MIN_SL_ATR = 0.50
MAX_SL_ATR = 1.80
MIN_CONFIRMATION_FAMILIES = 5
MAX_ATR_PERCENTILE = 95.0
MIN_ATR_PERCENTILE = 20.0


def _f(value: Any, default: float = 0.0) -> float:
    try: return float(value) if value is not None else default
    except (TypeError, ValueError): return default


def validate_analysis(data: dict[str, Any], *, min_confluence: int, min_rr: float, require_increasing_volume: bool = False) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    side = str(data.get("setup") or "").upper()
    if side not in {"LONG", "SHORT"}: reasons.append("No deterministic LONG/SHORT setup")
    score = int(_f(data.get("score"), 0)); required_score = max(MIN_SCORE, int(min_confluence or 0))
    if score < required_score: reasons.append(f"Score {score}/100 < required {required_score}")
    rr = _f(data.get("rr"), 0.0); required_rr = max(MIN_RR, float(min_rr or 0))
    if rr < required_rr: reasons.append(f"RR {rr:.2f} < required {required_rr:.2f}")
    for key, message in (
        ("direction_ok", "4H/1H directional alignment failed"),
        ("structure_ok", "Market structure confirmation failed"),
        ("setup_ok", "15M setup / 5M trigger failed"),
        ("momentum_ok", "Momentum confirmation failed"),
        ("volume_ok", "Multi-timeframe volume confirmation failed"),
        ("location_ok", "Location / structural target path failed"),
        ("futures_ok", "Futures market context failed"),
        ("volatility_ok", "Volatility hard gate failed"),
        ("btc_filter_ok", "BTC/global filter failed"),
        ("data_fresh", "Market data is stale"),
        ("target_path_structural", "Targets are not backed by structural/liquidity levels"),
    ):
        if not bool(data.get(key, False)): reasons.append(message)

    if not bool(data.get("five_minute_ready", False)): reasons.append("5M data is not ready")
    if side == "LONG" and not bool(data.get("five_minute_long", False)): reasons.append("5M LONG trigger confirmation failed")
    if side == "SHORT" and not bool(data.get("five_minute_short", False)): reasons.append("5M SHORT trigger confirmation failed")
    if bool(data.get("five_minute_long")) and bool(data.get("five_minute_short")): reasons.append("Conflicting 5M triggers")

    families = int(_f(data.get("confirmation_family_count"), 0))
    if families < MIN_CONFIRMATION_FAMILIES: reasons.append(f"Confirmation families {families}/6 < required {MIN_CONFIRMATION_FAMILIES}/6")

    trigger_q = _f(data.get("trigger_quality_5m"), 0.0)
    if trigger_q < 0.55: reasons.append(f"5M trigger quality {trigger_q:.2f} < 0.55")
    r15 = _f(data.get("rsi"), 50.0); r5 = _f(data.get("rsi_5m"), 50.0)
    if side == "LONG":
        if r15 <= 50 or r5 <= 50: reasons.append("RSI does not confirm LONG")
        if r5 >= 75: reasons.append("5M RSI is excessively extended")
    elif side == "SHORT":
        if r15 >= 50 or r5 >= 50: reasons.append("RSI does not confirm SHORT")
        if r5 <= 25: reasons.append("5M RSI is excessively extended")

    if _f(data.get("rvol_15m"), 0.0) < 1.0: reasons.append("15M RVOL < 1.0")
    if _f(data.get("rvol_5m"), 0.0) < 1.0: reasons.append("5M RVOL < 1.0")
    if require_increasing_volume and str(data.get("volume") or "").upper() != "INCREASING": reasons.append("15M volume is not INCREASING")

    sl_atr = _f(data.get("sl_atr"), 999.0)
    if sl_atr < MIN_SL_ATR: reasons.append(f"SL distance {sl_atr:.2f} ATR < minimum {MIN_SL_ATR:.2f}")
    if sl_atr > MAX_SL_ATR: reasons.append(f"SL distance {sl_atr:.2f} ATR > maximum {MAX_SL_ATR:.2f}")
    atr_rank = _f(data.get("atr_percentile"), 50.0)
    if atr_rank < MIN_ATR_PERCENTILE or atr_rank > MAX_ATR_PERCENTILE: reasons.append("ATR volatility percentile outside allowed range")
    spread = _f(data.get("mexc_spread_pct"), 999.0)
    if spread > _f(data.get("max_mexc_spread_pct"), 0.001): reasons.append("MEXC spread gate failed")
    drift = _f(data.get("entry_drift_pct"), 0.0)
    if drift > _f(data.get("max_entry_drift_pct"), 0.002): reasons.append("Entry drift gate failed")
    if bool(data.get("signal_blocked", False)) and not bool(data.get("technical_candidate", False)): reasons.append("Engine marked signal blocked")
    if not data.get("entry") or not data.get("stop_loss") or not data.get("tp1") or not data.get("tp2"): reasons.append("Trade levels are incomplete")
    return len(reasons) == 0, reasons
