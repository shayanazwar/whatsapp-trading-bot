from __future__ import annotations

import math


def assess_orderbook_capacity(*, depth: dict, side: str, mid_price: float,
                               contract_size: float, planned_notional_usdt: float,
                               max_distance_bps: float = 25.0,
                               minimum_depth_multiple: float = 1.0) -> dict:
    """Estimate immediately visible executable-side depth from MEXC contract depth.

    MEXC Futures depth levels are [price, order_count, contract_quantity].
    LONG entries consume asks; SHORT entries consume bids. Only levels within
    `max_distance_bps` of mid are counted. This is a snapshot filter, not a fill
    simulator or guarantee that displayed liquidity remains available.
    """
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    numeric = (mid_price, contract_size, planned_notional_usdt,
               max_distance_bps, minimum_depth_multiple)
    if not all(math.isfinite(float(x)) for x in numeric):
        raise ValueError("Order-book capacity inputs must be finite")
    if mid_price <= 0 or contract_size <= 0 or planned_notional_usdt <= 0:
        raise ValueError("Mid price, contract size and planned notional must be positive")
    if max_distance_bps <= 0 or minimum_depth_multiple <= 0:
        raise ValueError("Depth distance and minimum multiple must be positive")
    key = "asks" if side == "LONG" else "bids"
    levels = depth.get(key)
    if not isinstance(levels, list) or not levels:
        raise ValueError(f"MEXC order book has no {key} levels")

    parsed: list[tuple[float, float]] = []
    for index, level in enumerate(levels):
        if not isinstance(level, (list, tuple)) or len(level) < 3:
            raise ValueError(f"Malformed MEXC {key} depth level at index {index}")
        try:
            price = float(level[0])
            contracts = float(level[2])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Malformed MEXC {key} depth values at index {index}") from exc
        if not math.isfinite(price) or not math.isfinite(contracts) or price <= 0 or contracts < 0:
            raise ValueError(f"Invalid MEXC {key} depth values at index {index}")
        distance = ((price - mid_price) / mid_price * 10_000
                    if side == "LONG" else (mid_price - price) / mid_price * 10_000)
        # A crossed/inside-mid level is retained; levels on the wrong side are not.
        if distance < -1e-6:
            raise ValueError(f"MEXC {key} level is on the wrong side of mid price")
        if distance <= max_distance_bps and contracts > 0:
            parsed.append((price, contracts))

    depth_notional = sum(price * contracts * contract_size for price, contracts in parsed)
    multiple = depth_notional / planned_notional_usdt
    approved = multiple >= minimum_depth_multiple
    return {
        "side_consumed": key,
        "levels_counted": len(parsed),
        "depth_notional_usdt_within_band": float(depth_notional),
        "planned_notional_usdt": float(planned_notional_usdt),
        "depth_to_order_multiple": float(multiple),
        "max_distance_bps": float(max_distance_bps),
        "minimum_depth_multiple": float(minimum_depth_multiple),
        "approved": bool(approved),
        "interpretation": "VISIBLE_SNAPSHOT_DEPTH_NOT_A_FILL_OR_MARKET_IMPACT_GUARANTEE",
    }
