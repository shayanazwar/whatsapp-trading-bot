from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any

from .risk_manager import MIN_RR, MIN_SL_ATR, MAX_SL_ATR, MIN_TP_ATR, TradePlan, calculate_rr_after_costs, validate_levels
from .setup_filter import validate_analysis

ONE_HOUR_MS = 3_600_000
DEFAULT_MAX_SIGNAL_AGE_MS = 120 * 1000


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
    setup_identity: str | None = None,
) -> str:
    """Build a deterministic signal key.

    V11 setup identity is based on the actual impulse/sweep/reclaim event.
    Legacy BOS fields remain supported for older callers and tests.
    """
    if setup_identity:
        raw = f"mexc|{symbol.upper()}|{side.upper()}|V11SETUP:{setup_identity}"
    else:
        bos_time = setup_bos_time if setup_bos_time is not None else candle_time
        try:
            level_key = f"{float(bos_level):.12g}" if bos_level is not None else "NA"
        except (TypeError, ValueError):
            level_key = "NA"
        raw = f"mexc|{symbol.upper()}|{side.upper()}|SETUP:{bos_time}|LEVEL:{level_key}"
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
        tp1 = float(data["tp1"])
        tp2 = float(data["tp2"])
        return tp2 if abs(tp1 - tp2) <= max(1e-12, abs(tp2) * 1e-9) else None
    except (KeyError, TypeError, ValueError):
        return None


def validate_signal(
    data: dict[str, Any],
    *,
    min_confluence: int,
    min_rr: float,
    require_increasing_volume: bool = False,
) -> tuple[ValidatedSignal | None, list[str]]:
    ok, reasons = validate_analysis(
        data,
        min_confluence=min_confluence,
        min_rr=min_rr,
        require_increasing_volume=require_increasing_volume,
    )
    reasons = [str(reason) for reason in (reasons or [])]
    if not ok:
        return None, reasons

    side = str(data.get("setup") or "").upper()
    symbol = str(data.get("symbol") or "").strip().upper()
    if side not in {"LONG", "SHORT"}:
        return None, ["Invalid LONG/SHORT setup"]
    if not symbol:
        return None, ["Missing symbol"]

    candle_time = _ms(data.get("candle_time"))
    if candle_time is None:
        return None, ["Missing normalized 1H candle timestamp"]

    now_ms = int(time.time() * 1000)
    if candle_time > now_ms + ONE_HOUR_MS:
        return None, ["1H candle timestamp is in the future"]

    try:
        configured_age = data.get("max_signal_age_seconds")
        max_age_ms = (
            int(float(configured_age) * 1000)
            if configured_age is not None
            else DEFAULT_MAX_SIGNAL_AGE_MS
        )
        max_age_ms = max(5_000, max_age_ms)
    except (TypeError, ValueError):
        max_age_ms = DEFAULT_MAX_SIGNAL_AGE_MS

    signal_age_ms = now_ms - candle_time
    if signal_age_ms > max_age_ms:
        return None, [f"1H setup age exceeds {max_age_ms / 1000:.0f}s"]
    if signal_age_ms < 0:
        return None, ["1H candle timestamp is unexpectedly in the future"]

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
    # Live and backtest use the same fixed conservative allowance. Do not
    # mutate RR based on the instantaneous funding quote, because that would
    # make acceptance rules differ between historical and live execution.
    try:
        round_trip_cost_pct = data.get("effective_round_trip_cost_pct")
        if round_trip_cost_pct is None:
            base_cost = float(data.get("estimated_round_trip_cost_pct", 0.0012) or 0.0012)
            funding_allowance = float(data.get("estimated_funding_cost_pct", 0.0002) or 0.0002)
            # Keep the fallback identical to Settings.effective_round_trip_cost_pct:
            # two fee legs + two adverse-slippage legs, plus one fixed funding allowance.
            backtest_execution_cost = (2.0 * 0.0006) + (2.0 * 2.0 / 10_000.0)
            round_trip_cost_pct = max(0.0, base_cost, backtest_execution_cost) + max(0.0, funding_allowance)
        round_trip_cost_pct = max(0.0, float(round_trip_cost_pct))
    except (TypeError, ValueError):
        return None, ["Invalid shared round-trip cost model"]

    try:
        rr_gross = abs(tp - entry) / abs(entry - stop_loss)
        rr_net = calculate_rr_after_costs(
            side=side,
            entry=entry,
            stop_loss=stop_loss,
            target=tp,
            round_trip_cost_pct=round_trip_cost_pct,
        )
    except (TypeError, ValueError, ZeroDivisionError):
        return None, ["Unable to calculate post-cost RR"]

    if rr_net + 1e-12 < required_rr:
        return None, [
            f"Post-cost RR {rr_net:.2f} < required {required_rr:.2f} (gross {rr_gross:.2f})"
        ]

    try:
        atr = float(data.get("atr_4h") or data.get("atr") or 0.0)
    except (TypeError, ValueError):
        atr = 0.0
    if not math.isfinite(atr) or atr <= 0:
        return None, ["ATR is missing or non-positive"]
    # Recompute geometry from the actual validated levels so stale cached
    # diagnostics can never override the authoritative Entry/SL/TP values.
    sl_atr = abs(entry - stop_loss) / atr
    tp_atr = abs(tp - entry) / atr
    if sl_atr < MIN_SL_ATR or sl_atr > MAX_SL_ATR:
        return None, [f"SL distance {sl_atr:.2f} ATR outside safety bounds"]
    if tp_atr < MIN_TP_ATR:
        return None, [f"TP distance {tp_atr:.2f} ATR < minimum {MIN_TP_ATR:.2f}"]

    plan = TradePlan(side=side, entry=entry, stop_loss=stop_loss, tp=tp, rr=rr_net)
    level_ok, level_reason = validate_levels(plan, min_rr=required_rr)
    if not level_ok:
        return None, [str(level_reason)]

    setup_bos_time = _ms(data.get("setup_bos_time"))
    bos_level = data.get("bos_4h_level")
    if bos_level is None:
        bos_level = data.get("long_bos_level") if side == "LONG" else data.get("short_bos_level")
    key = make_signal_key(
        symbol,
        side,
        candle_time,
        setup_bos_time=setup_bos_time,
        bos_level=bos_level,
        setup_identity=str(data.get("setup_identity") or "").strip() or None,
    )

    analysis = dict(data)
    analysis.update(
        {
            "tp": tp,
            "rr": rr_net,
            "rr_gross": rr_gross,
            "rr_net": rr_net,
            "estimated_round_trip_cost_pct": round_trip_cost_pct,
            "sl_atr": sl_atr,
            "tp_distance_atr": tp_atr,
            "tp_distance_pct": abs(tp - entry) / entry,
            "primary_entry_timeframe": "1H",
            "signal_candle_timeframe": "1H",
        }
    )
    analysis.pop("tp1", None)
    analysis.pop("tp2", None)

    return ValidatedSignal(key=key, symbol=symbol, side=side, candle_time=candle_time, analysis=analysis, plan=plan), []
