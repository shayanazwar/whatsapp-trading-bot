from __future__ import annotations
from datetime import datetime, timezone
import math

from app.risk.manager import quantize_price


def refresh_trade_levels(*, ticker: dict, contract: dict, side: str, reference_entry: float,
                         stop: float, atr: float, reference_tp1: float | None = None,
                         reference_tp2: float | None = None, max_age_seconds: float = 15.0, max_entry_drift_atr: float = 0.5,
                         max_spread_bps: float | None = None,
                         tp2_r_multiple: float = 2.5,
                         now: datetime | None = None) -> dict:
    """Fail-closed quote refresh and adverse-side tick rounding before paper approval."""
    if side not in {"LONG", "SHORT"}:
        raise ValueError("Invalid side")
    if not math.isfinite(tp2_r_multiple) or not 1.0 < tp2_r_multiple <= 10.0:
        raise ValueError("tp2_r_multiple must be finite and in (1, 10]")
    now = now or datetime.now(timezone.utc)
    ts_raw = ticker.get("timestamp")
    if ts_raw is None:
        raise ValueError("Fresh ticker timestamp is missing")
    quote_time = datetime.fromtimestamp(float(ts_raw) / 1000.0, tz=timezone.utc)
    age = (now - quote_time).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise ValueError(f"Ticker is stale or future-dated ({age:.2f}s old)")
    bid, ask = float(ticker.get("bid1") or 0), float(ticker.get("ask1") or 0)
    tick = float(contract.get("priceUnit") or 0)
    if bid <= 0 or ask <= 0 or ask < bid or tick <= 0:
        raise ValueError("Fresh bid/ask or contract priceUnit is invalid")
    spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
    if max_spread_bps is not None and spread_bps > max_spread_bps:
        raise ValueError(f"Spread {spread_bps:.2f} bps exceeds limit {max_spread_bps:.2f}")
    entry_raw = ask if side == "LONG" else bid
    mark_raw = ticker.get("fairPrice") or ticker.get("markPrice") or ticker.get("lastPrice")
    try:
        mark_price = float(mark_raw) if mark_raw is not None else None
    except (TypeError, ValueError):
        mark_price = None
    if mark_price is not None and (not math.isfinite(mark_price) or mark_price <= 0):
        raise ValueError("Ticker mark/last price is invalid")
    # Reject if either ORIGINAL target has already traded. Recalculating new
    # targets from the refreshed price first would make this check tautological.
    if reference_tp1 is not None:
        if not math.isfinite(reference_tp1) or reference_tp1 <= 0:
            raise ValueError("reference_tp1 must be finite and positive")
        if side == "LONG" and bid >= reference_tp1:
            raise ValueError("Original TP1 has already been reached by the refreshed quote")
        if side == "SHORT" and ask <= reference_tp1:
            raise ValueError("Original TP1 has already been reached by the refreshed quote")
    if reference_tp2 is not None:
        if not math.isfinite(reference_tp2) or reference_tp2 <= 0:
            raise ValueError("reference_tp2 must be finite and positive")
        if side == "LONG" and bid >= reference_tp2:
            raise ValueError("Original TP2 has already been reached by the refreshed quote")
        if side == "SHORT" and ask <= reference_tp2:
            raise ValueError("Original TP2 has already been reached by the refreshed quote")
    if atr <= 0 or not math.isfinite(atr):
        raise ValueError("ATR must be finite and positive for entry drift validation")
    drift_atr = abs(entry_raw - reference_entry) / atr
    if drift_atr > max_entry_drift_atr:
        raise ValueError(f"Entry drift {drift_atr:.3f} ATR exceeds limit {max_entry_drift_atr:.3f}")

    entry = quantize_price(entry_raw, tick, "ceil" if side == "LONG" else "floor")
    stop_price = quantize_price(stop, tick, "floor" if side == "LONG" else "ceil")
    if side == "LONG":
        if entry <= stop_price or entry_raw <= stop_price or (mark_price is not None and mark_price <= stop_price):
            raise ValueError("Long stop has already been crossed by the refreshed quote/mark")
        risk = entry - stop_price
        tp1 = quantize_price(entry + risk, tick, "ceil")
        tp2 = quantize_price(entry + tp2_r_multiple * risk, tick, "ceil")
    else:
        if entry >= stop_price or entry_raw >= stop_price or (mark_price is not None and mark_price >= stop_price):
            raise ValueError("Short stop has already been crossed by the refreshed quote/mark")
        risk = stop_price - entry
        tp1 = quantize_price(entry - risk, tick, "floor")
        tp2 = quantize_price(entry - tp2_r_multiple * risk, tick, "floor")
        if tp2 <= 0:
            raise ValueError("Short target is non-positive")
    return {
        "entry": entry, "stop": stop_price, "tp1": tp1, "tp2": tp2,
        "quote_age_seconds": age, "entry_drift_atr": drift_atr, "spread_bps": spread_bps,
        "quote_timestamp": quote_time.isoformat(), "reference_entry": reference_entry,
        "mark_price": mark_price,
    }
