from __future__ import annotations


def net_reward_risk(*, entry: float, stop: float, target: float, side: str,
                    contract_size: float, contracts: float, taker_fee_rate: float,
                    slippage_bps: float = 3) -> dict:
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    gross_risk_per_contract = abs(entry - stop) * contract_size
    gross_reward_per_contract = abs(target - entry) * contract_size
    if gross_risk_per_contract <= 0 or contracts <= 0:
        raise ValueError("Invalid position geometry")
    entry_notional = entry * contract_size * contracts
    stop_notional = stop * contract_size * contracts
    target_notional = target * contract_size * contracts
    fee_at_stop = (entry_notional + stop_notional) * taker_fee_rate
    fee_at_target = (entry_notional + target_notional) * taker_fee_rate
    slippage_at_stop = (entry_notional + stop_notional) * slippage_bps / 10_000
    slippage_at_target = (entry_notional + target_notional) * slippage_bps / 10_000
    risk_total = gross_risk_per_contract * contracts + fee_at_stop + slippage_at_stop
    reward_total = gross_reward_per_contract * contracts - fee_at_target - slippage_at_target
    return {
        "gross_rr": gross_reward_per_contract / gross_risk_per_contract,
        "net_rr": reward_total / risk_total if risk_total > 0 else 0,
        "stop_cost_usdt": fee_at_stop + slippage_at_stop,
        "target_cost_usdt": fee_at_target + slippage_at_target,
        "estimated_net_reward_usdt": reward_total,
        "estimated_risk_usdt": risk_total,
    }
