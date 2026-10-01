from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any

from .risk_manager import TradePlan, calculate_rr, validate_levels
from .setup_filter import validate_analysis


MIN_SCORE = 75
MIN_RR = 2.0
MIN_CONFIRMATION_FAMILIES = 4

FIFTEEN_MINUTE_MS = 900_000
DEFAULT_MAX_SIGNAL_AGE_MS = 20 * 60 * 1000

_IGNORED_FIVE_MINUTE_MARKERS = {
    "5m_trigger_confirmation",
    "5m_trigger",
    "5m_confirmation",
    "5m_entry_confirmation",
}


@dataclass(frozen=True)
class ValidatedSignal:
    key: str
    symbol: str
    side: str
    candle_time: int
    analysis: dict[str, Any]
    plan: TradePlan


def make_signal_key(symbol: str, side: str, candle_time: int) -> str:
    raw = f"mexc|{symbol.upper()}|{side.upper()}|{int(candle_time)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _ms(value: Any) -> int | None:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return None
    return v * 1000 if v < 10**12 else v


def _normalized_reason(reason: Any) -> str:
    raw = str(reason or "").strip().lower()
    return raw.replace(" ", "_").replace("-", "_").replace("/", "_")


def _five_minute_only_failure(reasons: list[str]) -> bool:
    if not reasons:
        return False
    return all(
        _normalized_reason(reason) in _IGNORED_FIVE_MINUTE_MARKERS
        or "5m_trigger" in _normalized_reason(reason)
        for reason in reasons
    )


def _filter_ignored_reasons(reasons: list[str]) -> list[str]:
    filtered: list[str] = []
    for reason in reasons:
        normalized = _normalized_reason(reason)
        if normalized in _IGNORED_FIVE_MINUTE_MARKERS or "5m_trigger" in normalized:
            continue
        filtered.append(str(reason))
    return filtered


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
        non_5m_reasons = _filter_ignored_reasons(reasons)
        if _five_minute_only_failure(reasons):
            ok = True
            reasons = []
        elif non_5m_reasons:
            return None, non_5m_reasons
        else:
            return None, reasons or ["Technical analysis rejected the setup"]

    side = str(data.get("setup") or "").upper()
    if side not in {"LONG", "SHORT"}:
        return None, ["Invalid LONG/SHORT setup"]

    symbol = str(data.get("symbol") or "").strip().upper()
    if not symbol:
        return None, ["Missing symbol"]

    # 15M is the authoritative setup clock. No 5M trigger/freshness field is
    # required and closed_5m_candle_time is intentionally ignored.
    candle_time = _ms(data.get("candle_time"))
    if candle_time is None:
        return None, ["Missing normalized 15M candle timestamp"]
    now_ms = int(time.time() * 1000)
    if candle_time > now_ms + FIFTEEN_MINUTE_MS:
        return None, ["15M candle timestamp is in the future"]

    configured_age_seconds = data.get("max_signal_age_seconds")
    try:
        max_age_ms = int(
            float(
                configured_age_seconds
                if configured_age_seconds is not None
                else DEFAULT_MAX_SIGNAL_AGE_MS / 1000.0
            )
            * 1000
        )
    except (TypeError, ValueError):
        max_age_ms = DEFAULT_MAX_SIGNAL_AGE_MS

    if max_age_ms <= 0:
        max_age_ms = DEFAULT_MAX_SIGNAL_AGE_MS
    # Prevent a stale legacy 5M default from silently reintroducing a 5M gate.
    max_age_ms = max(max_age_ms, FIFTEEN_MINUTE_MS + 5 * 60 * 1000)

    signal_age_ms = now_ms - candle_time
    if signal_age_ms > max_age_ms:
        return None, [f"15M setup age exceeds {max_age_ms / 1000:.0f}s"]
    if signal_age_ms < 0:
        return None, ["15M candle timestamp is unexpectedly in the future"]

    try:
        entry = float(data["entry"])
        stop_loss = float(data["stop_loss"])
        tp1 = float(data["tp1"])
        tp2 = float(data["tp2"])
    except (KeyError, TypeError, ValueError):
        return None, ["Invalid or missing trade levels"]

    if not all(math.isfinite(value) and value > 0 for value in (entry, stop_loss, tp1, tp2)):
        return None, ["Trade levels must be finite and positive"]

    if side == "LONG":
        if stop_loss >= entry:
            return None, ["LONG stop loss must be below entry"]
        if tp1 <= entry:
            return None, ["LONG TP1 must be above entry"]
        if tp2 <= entry:
            return None, ["LONG TP2 must be above entry"]
    else:
        if stop_loss <= entry:
            return None, ["SHORT stop loss must be above entry"]
        if tp1 >= entry:
            return None, ["SHORT TP1 must be below entry"]
        if tp2 >= entry:
            return None, ["SHORT TP2 must be below entry"]

    try:
        calculated_rr = calculate_rr(
            side=side,
            entry=entry,
            stop_loss=stop_loss,
            target=tp2,
        )
        required_rr = max(MIN_RR, float(min_rr))
    except (TypeError, ValueError) as exc:
        return None, [f"Invalid trade levels: {exc}"]

    if not math.isfinite(required_rr) or required_rr <= 0:
        return None, ["Minimum RR must be finite and positive"]
    if calculated_rr + 1e-12 < required_rr:
        return None, [f"Calculated RR {calculated_rr:.2f} < required {required_rr:.2f}"]

    plan = TradePlan(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        rr=calculated_rr,
    )

    level_ok, level_reason = validate_levels(plan, min_rr=required_rr)
    if not level_ok:
        return None, [level_reason]

    # The 15M setup timestamp is the identity clock because it is now the only
    # mandatory technical trigger timeframe.
    key = make_signal_key(symbol, side, candle_time)
    analysis = dict(data)
    analysis.pop("closed_5m_candle_time", None)
    analysis["primary_entry_timeframe"] = "15M"
    analysis["five_minute_confirmation_bypassed"] = True
    analysis["signal_candle_timeframe"] = "15M"

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
