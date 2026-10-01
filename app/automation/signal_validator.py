from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any

from .risk_manager import (
    MIN_RR,
    MIN_SL_ATR,
    MAX_SL_ATR,
    MIN_TP_ATR,
    TradePlan,
    calculate_rr_after_costs,
    validate_levels,
)
from .setup_filter import (
    MIN_CONFIRMATION_FAMILIES,
    MIN_AVAILABLE_CONFIRMATION_FAMILIES,
    validate_analysis,
)

FIFTEEN_MINUTE_MS = 900_000
DEFAULT_MAX_SIGNAL_AGE_MS = 20 * 60 * 1000


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
    candle_time: int | None = None,
    *,
    setup_bos_time: int | None = None,
    bos_level: float | None = None,
) -> str:
    """Identity is the structural setup, not the current scan price."""
    bos_time = setup_bos_time if setup_bos_time is not None else candle_time
    try:
        level_key = f"{float(bos_level):.12g}" if bos_level is not None else "NA"
    except (TypeError, ValueError):
        level_key = "NA"
    raw = f"mexc|{symbol.upper()}|{side.upper()}|BOS:{bos_time}|LEVEL:{level_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _ms(value: Any) -> int | None:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return None
    return v * 1000 if v < 10**12 else v


def _single_tp(data: dict[str, Any]) -> float | None:
    try:
        if data.get("tp") is not None:
            return float(data["tp"])
        # Read old persisted payloads, but require a single unambiguous target.
        tp1 = float(data["tp1"])
        tp2 = float(data["tp2"])
        if abs(tp1 - tp2) > max(1e-12, abs(tp2) * 1e-9):
            return None
        return tp2
    except (KeyError, TypeError, ValueError):
        return None


def validate_signal(
    data: dict[str, Any],
    *,
    min_confluence: int,
    min_rr: float,
    require_increasing_volume: bool,
) -> tuple[ValidatedSignal | None, list[str]]:
    ok, reasons = validate_analysis(
        data,
        min_confluence=min_confluence,
        min_rr=min_rr,
        require_increasing_volume=require_increasing_volume,
    )
    reasons = [str(reason) for reason in (reasons or [])]
    if not ok:
        non_5m_reasons = [reason for reason in reasons if "5m" not in reason.lower() and "five_minute" not in reason.lower()]
        if non_5m_reasons:
            return None, non_5m_reasons
        # 5M is optional by architecture; a legacy 5M-only rejection cannot
        # invalidate an otherwise valid 15M structural signal.
        ok = True
        reasons = []

    side = str(data.get("setup") or "").upper()
    symbol = str(data.get("symbol") or "").strip().upper()
    if side not in {"LONG", "SHORT"}:
        return None, ["Invalid LONG/SHORT setup"]
    if not symbol:
        return None, ["Missing symbol"]

    candle_time = _ms(data.get("candle_time"))
    if candle_time is None:
        return None, ["Missing normalized 15M candle timestamp"]

    now_ms = int(time.time() * 1000)
    if candle_time > now_ms + FIFTEEN_MINUTE_MS:
        return None, ["15M candle timestamp is in the future"]

    try:
        configured_age = (
            data.get("max_signal_age_seconds")
            if data.get("max_signal_age_seconds") is not None
            else DEFAULT_MAX_SIGNAL_AGE_MS / 1000.0
        )
        max_age_ms = max(
            FIFTEEN_MINUTE_MS,
            int(float(configured_age) * 1000),
        )
    except (TypeError, ValueError):
        max_age_ms = DEFAULT_MAX_SIGNAL_AGE_MS

    signal_age_ms = now_ms - candle_time
    if signal_age_ms > max_age_ms:
        return None, [f"15M setup age exceeds {max_age_ms / 1000:.0f}s"]
    if signal_age_ms < 0:
        return None, ["15M candle timestamp is unexpectedly in the future"]

    try:
        entry = float(data["entry"])
        stop_loss = float(data["stop_loss"])
    except (KeyError, TypeError, ValueError):
        return None, ["Invalid or missing entry/SL"]

    tp = _single_tp(data)
    if not all(math.isfinite(v) and v > 0 for v in (entry, stop_loss, tp or 0.0)):
        return None, ["Trade levels must be finite and positive"]
    assert tp is not None

    if side == "LONG" and not (stop_loss < entry < tp):
        return None, ["LONG geometry must satisfy SL < Entry < TP"]
    if side == "SHORT" and not (tp < entry < stop_loss):
        return None, ["SHORT geometry must satisfy TP < Entry < SL"]

    required_rr = max(MIN_RR, float(min_rr or 0.0))
    round_trip_cost_pct = float(
        data.get("estimated_round_trip_cost_pct", 0.0015) or 0.0015
    )
    funding = data.get("mexc_funding_rate")
    if funding is not None:
        # Conservative allowance for up to two funding intervals.
        round_trip_cost_pct += min(0.0010, abs(float(funding)) * 2.0)

    try:
        rr_gross = abs(tp - entry) / abs(entry - stop_loss)
        rr_net = calculate_rr_after_costs(
            side=side,
            entry=entry,
            stop_loss=stop_loss,
            target=tp,
            round_trip_cost_pct=round_trip_cost_pct,
        )
    except (TypeError, ValueError):
        return None, ["Unable to calculate post-cost RR"]

    if rr_net + 1e-12 < required_rr:
        return None, [
            f"Post-cost RR {rr_net:.2f} < required {required_rr:.2f} "
            f"(gross {rr_gross:.2f})"
        ]

    atr = float(data.get("atr") or 0.0)
    sl_atr = abs(entry - stop_loss) / atr if atr > 0 else 0.0
    tp_atr = abs(tp - entry) / atr if atr > 0 else 0.0
    if sl_atr < MIN_SL_ATR or sl_atr > MAX_SL_ATR:
        return None, [f"SL distance {sl_atr:.2f} ATR outside safety bounds"]
    if tp_atr < MIN_TP_ATR:
        return None, [f"TP distance {tp_atr:.2f} ATR < minimum {MIN_TP_ATR:.2f}"]

    plan = TradePlan(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        tp=tp,
        rr=rr_net,
    )
    level_ok, level_reason = validate_levels(plan, min_rr=required_rr)
    if not level_ok:
        return None, [level_reason]

    setup_bos_time = _ms(data.get("setup_bos_time"))
    key = make_signal_key(
        symbol,
        side,
        candle_time,
        setup_bos_time=setup_bos_time,
        bos_level=data.get("bos_15m_level", data.get("long_bos_level") if side == "LONG" else data.get("short_bos_level")),
    )
    analysis = dict(data)
    analysis["tp"] = tp
    analysis["rr"] = rr_net
    analysis["rr_gross"] = rr_gross
    analysis["rr_net"] = rr_net
    analysis["estimated_round_trip_cost_pct"] = round_trip_cost_pct
    analysis["tp_distance_atr"] = tp_atr
    analysis["tp_distance_pct"] = abs(tp - entry) / entry
    analysis["primary_entry_timeframe"] = "15M"
    analysis["signal_candle_timeframe"] = "15M"
    analysis["five_minute_confirmation_bypassed"] = True
    # Remove legacy multi-stage fields from the user-facing analysis payload.
    analysis.pop("tp1", None)
    analysis.pop("tp2", None)
    analysis.pop("closed_5m_candle_time", None)

    return (
        ValidatedSignal(
            key=key,
            symbol=symbol,
            side=side,
            candle_time=candle_time,
            analysis=analysis,
            plan=plan,
        ),
        [],
    )
