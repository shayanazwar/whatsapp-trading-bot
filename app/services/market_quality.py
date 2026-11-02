"""Live order-book quality checks for research-signal delivery.

Historical backtests do not invent historical depth. This module is used only to
validate a fresh scan candidate immediately before it is eligible for WhatsApp push.
"""
from __future__ import annotations

import math
from typing import Any, Mapping


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
