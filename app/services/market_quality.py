"""Live order-book quality checks for research-signal delivery.

Historical backtests do not invent historical depth. This module is used only to
validate a fresh scan candidate immediately before it is eligible for WhatsApp push.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


def _levels(value: Any, *, contract_size: float) -> tuple[float, float]:
    if not isinstance(value, list):
        raise ValueError("Order-book side is missing")
    best_price = 0.0
    notional = 0.0
    for row in value[:20]:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price, contracts = float(row[0]), float(row[1])
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(price) or not math.isfinite(contracts) or price <= 0 or contracts <= 0:
            continue
        if best_price <= 0:
            best_price = price
        notional += price * contracts * contract_size
    if best_price <= 0:
        raise ValueError("Order-book side has no valid levels")
    return best_price, notional


def assess_orderbook(
    payload: Mapping[str, Any], *, contract_size: float, atr_value: float,
    max_spread_atr_fraction: float, min_depth_usdt: float,
) -> dict[str, Any]:
    """Compute spread and top-20 notional depth in USDT; raise if data is unusable."""
    if contract_size <= 0 or atr_value <= 0:
        raise ValueError("Contract size and ATR must be positive")
    bid, bid_depth = _levels(payload.get("bids"), contract_size=contract_size)
    ask, ask_depth = _levels(payload.get("asks"), contract_size=contract_size)
    if ask <= bid:
        raise ValueError("Order book is crossed or locked")
    mid = (ask + bid) / 2.0
    spread = ask - bid
    spread_bps = spread / mid * 10_000.0
    spread_atr_fraction = spread / atr_value
    depth = min(bid_depth, ask_depth)
    reasons: list[str] = []
    if spread_atr_fraction > max_spread_atr_fraction:
        reasons.append("SPREAD_EXCEEDS_ATR_BUDGET")
    if min_depth_usdt > 0 and depth < min_depth_usdt:
        reasons.append("INSUFFICIENT_TOP20_DEPTH")
    return {
        "best_bid": bid,
        "best_ask": ask,
        "mid_price": mid,
        "spread_price": spread,
        "spread_bps": spread_bps,
        "spread_atr_fraction": spread_atr_fraction,
        "bid_depth_usdt_top20": bid_depth,
        "ask_depth_usdt_top20": ask_depth,
        "usable_depth_usdt": depth,
        "passed": not reasons,
        "reasons": reasons,
    }


def adverse_funding_rate(direction: str, funding_rate: float) -> float:
    """Positive rate charges longs and credits shorts; negative rate does the reverse."""
    if direction.upper() == "LONG":
        return max(0.0, funding_rate)
    if direction.upper() == "SHORT":
        return max(0.0, -funding_rate)
    raise ValueError("direction must be LONG or SHORT")


def net_reward_risk_after_costs(
    *, direction: str, entry_price: float, stop_price: float, target_price: float,
    fee_rate_per_side: float, slippage_bps_per_side: float,
) -> dict[str, float]:
    """Estimate per-unit net reward/risk using the backtest's two-sided cost model.

    This is a pre-entry screen, not a fill guarantee. Funding is excluded because
    the holding duration and future funding rates are unknown at signal time.
    """
    side = direction.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    values = (entry_price, stop_price, target_price, fee_rate_per_side, slippage_bps_per_side)
    if any(not math.isfinite(float(v)) for v in values):
        raise ValueError("price/cost inputs must be finite")
    if min(entry_price, stop_price, target_price) <= 0 or fee_rate_per_side < 0 or slippage_bps_per_side < 0:
        raise ValueError("price/cost inputs are outside valid ranges")
    sign = 1.0 if side == "LONG" else -1.0
    slip = slippage_bps_per_side / 10_000.0
    entry_fill = entry_price * (1.0 + slip if side == "LONG" else 1.0 - slip)

    def net_pnl(exit_reference: float) -> float:
        exit_fill = exit_reference * (1.0 - slip if side == "LONG" else 1.0 + slip)
        gross = (exit_fill - entry_fill) * sign
        fees = (entry_fill + exit_fill) * fee_rate_per_side
        return gross - fees

    reward = net_pnl(target_price)
    risk = -net_pnl(stop_price)
    ratio = reward / risk if reward > 0 and risk > 0 else 0.0
    return {"net_reward_per_unit": reward, "net_risk_per_unit": risk, "net_rr": ratio}


def validate_usdt_perpetual_contract(
    spec: Mapping[str, Any] | None, *, margin_mode: str = "cross",
) -> tuple[bool, str]:
    """Fail-closed validation for metadata used to size USDT perpetual positions."""
    if not isinstance(spec, Mapping) or not spec:
        return False, "CONTRACT_METADATA_MISSING"

    def positive_number(*keys: str) -> float | None:
        for key in keys:
            value = spec.get(key)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(number) and number > 0:
                return number
        return None

    state = spec.get("state")
    try:
        if state is None or int(state) != 0:
            return False, "CONTRACT_NOT_VERIFIED_ACTIVE"
    except (TypeError, ValueError, OverflowError):
        return False, "CONTRACT_STATE_INVALID"

    future_type = spec.get("futureType")
    if future_type is None:
        return False, "CONTRACT_METADATA_MISSING:futureType"
    try:
        if int(future_type) != 1:
            return False, "CONTRACT_NOT_USDT_PERPETUAL"
    except (TypeError, ValueError, OverflowError):
        return False, "CONTRACT_FUTURE_TYPE_INVALID"

    quote_raw, settle_raw = spec.get("quoteCoin"), spec.get("settleCoin")
    if quote_raw is None or settle_raw is None:
        return False, "CONTRACT_METADATA_MISSING:quoteCoin_or_settleCoin"
    if str(quote_raw).upper() != "USDT" or str(settle_raw).upper() != "USDT":
        return False, "CONTRACT_NOT_USDT_SETTLED"

    api_allowed = spec.get("apiAllowed")
    if api_allowed not in (True, 1, "1", "true", "True"):
        return False, "CONTRACT_API_NOT_ALLOWED"

    position_open_type = spec.get("positionOpenType")
    if position_open_type is None:
        return False, "CONTRACT_METADATA_MISSING:positionOpenType"
    try:
        open_type = int(position_open_type)
    except (TypeError, ValueError, OverflowError):
        return False, "CONTRACT_POSITION_MODE_INVALID"
    if margin_mode == "cross" and open_type not in {2, 3}:
        return False, "CONTRACT_CROSS_MARGIN_UNSUPPORTED"
    if margin_mode == "isolated" and open_type not in {1, 3}:
        return False, "CONTRACT_ISOLATED_MARGIN_UNSUPPORTED"
    if margin_mode not in {"cross", "isolated"}:
        return False, "CONTRACT_MARGIN_MODE_UNSUPPORTED"

    required = {
        "contractSize": ("contractSize", "contract_size"),
        "volUnit": ("volUnit", "volumeUnit", "quantityStep"),
        "minVol": ("minVol", "minVolume", "minOrderQty"),
        "maxVol": ("maxVol", "maxVolume", "maxOrderQty"),
        "priceUnit": ("priceUnit", "price_unit"),
        "maintenanceMarginRate": ("maintenanceMarginRate", "maintenance_margin_rate", "mmr"),
    }
    for label, keys in required.items():
        if positive_number(*keys) is None:
            return False, f"CONTRACT_METADATA_INVALID:{label}"
    min_volume = positive_number("minVol", "minVolume", "minOrderQty")
    max_volume = positive_number("maxVol", "maxVolume", "maxOrderQty")
    if min_volume is None or max_volume is None or max_volume < min_volume:
        return False, "CONTRACT_METADATA_INVALID:VOLUME_LIMITS"
    leverage = positive_number("countryConfigContractMaxLeverage", "maxLeverage", "max_leverage")
    if leverage is None or leverage < 1:
        return False, "CONTRACT_METADATA_INVALID:MAX_LEVERAGE"
    return True, "OK"


def candle_window_coverage(
    timeframes: Mapping[str, Sequence[Any]], *, start_time: int, end_time: int,
) -> dict[str, dict[str, Any]]:
    """Measure exact UTC-grid coverage and continuity for 1D/12H/4H/1H bars.

    Bars overlapping the start boundary are included; only bars fully closed by
    ``end_time`` are counted. Any missing, duplicate, or off-grid open makes the
    timeframe discontinuous even when the percentage threshold would otherwise pass.
    """
    from app.market.candles import INTERVAL_SECONDS

    steps = {"1D": INTERVAL_SECONDS["1D"], "12H": INTERVAL_SECONDS["12H"],
             "4H": INTERVAL_SECONDS["4H"], "1H": INTERVAL_SECONDS["1H"]}
    if end_time <= start_time:
        raise ValueError("end_time must be later than start_time")
    report: dict[str, dict[str, Any]] = {}
    for timeframe, step in steps.items():
        first_open = (int(start_time) // step) * step
        last_open = ((int(end_time) // step) * step) - step
        expected_opens = list(range(first_open, last_open + 1, step)) if last_open >= first_open else []
        expected_set = set(expected_opens)
        observed_all: list[int] = []
        for candle in timeframes.get(timeframe, ()):
            try:
                opened = int(candle.open_time)
                closed = int(candle.close_time)
                complete = bool(candle.complete)
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
            if complete and first_open <= opened < last_open + step:
                # Count misaligned timestamps as defects even though their candle
                # naturally closes beyond the aligned test-window boundary.
                if opened not in expected_set or closed <= end_time:
                    observed_all.append(opened)
        duplicate_count = len(observed_all) - len(set(observed_all))
        off_grid = [opened for opened in observed_all if opened % step != 0 or opened not in expected_set]
        observed_set = set(observed_all) & expected_set
        missing = sorted(expected_set - observed_set)
        expected_count = len(expected_opens)
        observed_count = len(observed_set)
        coverage_pct = (min(100.0, observed_count / expected_count * 100.0)
                        if expected_count else 0.0)
        report[timeframe] = {
            "expected": expected_count,
            "observed": observed_count,
            "missing": len(missing),
            "duplicate": duplicate_count,
            "off_grid": len(off_grid),
            "coverage_pct": round(coverage_pct, 4),
            "continuous": bool(expected_count and not missing and not duplicate_count and not off_grid),
            "first_missing_open": missing[0] if missing else None,
        }
    return report


def signal_data_quality_gate(
    warnings: Sequence[str], contract: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Hard-veto signal generation when market data or instrument metadata is uncertain."""
    if warnings:
        return False, "MARKET_DATA_WARNINGS:" + ";".join(str(item) for item in warnings[:5])
    valid, reason = validate_usdt_perpetual_contract(contract)
    return (True, "OK") if valid else (False, reason)


def funding_events_for_window(
    events: Sequence[Mapping[str, Any]], *, history_complete: bool,
    requested_start: int, requested_end: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Slice a fetched wider funding history to one report window and assess coverage.

    A longer query is reusable for shorter reports: if the wider query itself is
    complete, all nested windows are covered. If it is truncated, the oldest
    returned settlement still proves coverage for any later start boundary it
    reaches. The function preserves the MEXC client's definition of coverage
    (history reaches the requested start); it does not assert gap-free settlements.
    """
    if requested_end < requested_start:
        raise ValueError("requested_end must not precede requested_start")
    normalized: list[tuple[int, dict[str, Any]]] = []
    for event in events:
        try:
            stamp = int(float(event.get("settleTime", event.get("settle_time", event.get("fundingTime", 0)))))
        except (TypeError, ValueError, OverflowError):
            continue
        if stamp >= 100_000_000_000:
            stamp //= 1000
        copied = dict(event)
        copied["settleTime"] = stamp
        normalized.append((stamp, copied))
    oldest = min((stamp for stamp, _ in normalized), default=None)
    covered = bool(history_complete or (oldest is not None and oldest <= int(requested_start)))
    selected = [row for stamp, row in sorted(normalized, key=lambda item: item[0])
                if int(requested_start) < stamp <= int(requested_end)]
    return selected, covered


def conservative_paper_funding_pnl(
    events: Sequence[Mapping[str, Any]], *, side: str, entry_time: int,
    candle_open_time: int, candle_close_time: int, entry_notional: float,
) -> float:
    """Accrue known settlements; only adverse funding is credited inside an exit bar."""
    direction = side.upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if not math.isfinite(float(entry_notional)) or entry_notional < 0 or candle_close_time <= candle_open_time:
        raise ValueError("invalid notional or candle interval")
    sign = 1.0 if direction == "LONG" else -1.0
    total = 0.0
    for event in events:
        try:
            settle = int(float(event.get("settleTime", event.get("settle_time", 0))))
            if settle >= 100_000_000_000:
                settle //= 1000
            rate = float(event.get("fundingRate", event.get("funding_rate", 0.0)))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(rate) or not int(entry_time) < settle <= int(candle_close_time):
            continue
        pnl = -sign * rate * float(entry_notional)
        if settle <= int(candle_open_time) or pnl < 0:
            total += pnl
    return total
