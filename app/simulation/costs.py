from __future__ import annotations

import math


def net_reward_risk(*, entry: float, stop: float, target: float, side: str,
                    contract_size: float, contracts: float, taker_fee_rate: float,
                    slippage_bps: float = 3, target_1: float | None = None,
                    target_1_fraction: float = 0.0) -> dict:
    """Calculate net R for the configured exit plan, not an all-at-TP2 fiction.

    `target` is the final target (TP2). If `target_1` is supplied, that fraction
    exits at TP1 and the remainder exits at TP2. Stop risk applies to the whole
    position. Fees/slippage include one full entry and the weighted partial exits.
    """
    vals = (entry, stop, target, contract_size, contracts, taker_fee_rate, slippage_bps,
            target_1_fraction) + ((target_1,) if target_1 is not None else ())
    if not all(math.isfinite(float(x)) for x in vals):
        raise ValueError("All reward/risk inputs must be finite")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    if entry <= 0 or stop <= 0 or target <= 0 or contract_size <= 0 or contracts <= 0:
        raise ValueError("Prices, contract size and contracts must be positive")
    if taker_fee_rate < 0 or taker_fee_rate >= 1 or slippage_bps < 0:
        raise ValueError("Fee rate or slippage assumption is invalid")
    if not 0 <= target_1_fraction < 1:
        raise ValueError("target_1_fraction must be in [0, 1)")

    if side == "LONG":
        if not stop < entry < target:
            raise ValueError("LONG requires stop < entry < final target")
        if target_1 is not None and not entry < target_1 <= target:
            raise ValueError("LONG TP1 must be above entry and no higher than TP2")
        gross_risk_per_unit = entry - stop
        reward_2_per_unit = target - entry
        reward_1_per_unit = (target_1 - entry) if target_1 is not None else reward_2_per_unit
    else:
        if not target < entry < stop:
            raise ValueError("SHORT requires final target < entry < stop")
        if target_1 is not None and not target <= target_1 < entry:
            raise ValueError("SHORT TP1 must be below entry and no lower than TP2")
        gross_risk_per_unit = stop - entry
        reward_2_per_unit = entry - target
        reward_1_per_unit = (entry - target_1) if target_1 is not None else reward_2_per_unit

    fraction_1 = target_1_fraction if target_1 is not None else 0.0
    fraction_2 = 1.0 - fraction_1
    gross_risk = gross_risk_per_unit * contract_size * contracts
    gross_reward = (fraction_1 * reward_1_per_unit + fraction_2 * reward_2_per_unit) * contract_size * contracts
    entry_notional = entry * contract_size * contracts
    stop_notional = stop * contract_size * contracts
    weighted_exit_price = fraction_1 * (target_1 if target_1 is not None else target) + fraction_2 * target
    reward_exit_notional = weighted_exit_price * contract_size * contracts
    risk_cost = (entry_notional + stop_notional) * (taker_fee_rate + slippage_bps / 10_000)
    reward_cost = (entry_notional + reward_exit_notional) * (taker_fee_rate + slippage_bps / 10_000)
    risk_total = gross_risk + risk_cost
    reward_total = gross_reward - reward_cost
    return {
        "gross_rr": gross_reward / gross_risk if gross_risk > 0 else 0.0,
        "net_rr": reward_total / risk_total if risk_total > 0 else 0.0,
        "gross_reward_usdt": gross_reward,
        "gross_risk_usdt": gross_risk,
        "stop_cost_usdt": risk_cost,
        "target_cost_usdt": reward_cost,
        "estimated_net_reward_usdt": reward_total,
        "estimated_risk_usdt": risk_total,
        "target_1_fraction": fraction_1,
        "target_2_fraction": fraction_2,
        "weighted_exit_price": weighted_exit_price,
        "exit_plan": "PARTIAL_TP1_THEN_TP2" if target_1 is not None else "FULL_POSITION_AT_FINAL_TARGET",
        "funding_included": False,
        "cost_basis": "AFTER_TAKER_FEES_AND_MODELED_SLIPPAGE_BEFORE_FUNDING",
    }
