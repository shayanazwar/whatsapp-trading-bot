from __future__ import annotations

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class RiskPlan:
    risk_budget_usdt: float
    contracts: float
    base_quantity: float
    notional_usdt: float
    estimated_stop_loss_usdt: float
    initial_margin_estimate_usdt: float
    liquidation_stress_price: float | None
    leverage: int
    notes: list[str]

    def as_dict(self):
        return asdict(self)


def size_position(*, equity: float, risk_pct: float, entry: float, stop: float, side: str,
                  contract_size: float, vol_unit: float, leverage: int,
                  taker_fee_rate: float | None, slippage_bps: float = 3,
                  maintenance_margin_rate: float | None = None) -> RiskPlan:
    if equity <= 0 or not (0 < risk_pct <= 5) or entry <= 0 or stop <= 0:
        raise ValueError("Invalid equity, risk percentage or entry/stop")
    if side not in {"LONG", "SHORT"} or contract_size <= 0 or vol_unit <= 0 or leverage < 1:
        raise ValueError("Invalid side, contract size, quantity step or leverage")
    distance = entry - stop if side == "LONG" else stop - entry
    if distance <= 0:
        raise ValueError("Stop must be on the losing side of entry")
    if taker_fee_rate is None or taker_fee_rate < 0:
        raise ValueError("Verified contract/account taker fee is required for net risk sizing")
    risk_budget = equity * risk_pct / 100
    round_trip_cost_per_contract = contract_size * (entry + stop) * taker_fee_rate
    slippage_cost_per_contract = contract_size * (entry + stop) * slippage_bps / 10_000
    loss_per_contract = distance * contract_size + round_trip_cost_per_contract + slippage_cost_per_contract
    raw_contracts = risk_budget / loss_per_contract
    contracts = math.floor(raw_contracts / vol_unit) * vol_unit
    if contracts <= 0:
        raise ValueError("Risk budget is too small for the contract's minimum quantity step")
    qty = contracts * contract_size
    notional = qty * entry
    estimated_loss = contracts * loss_per_contract
    margin = notional / leverage
    notes = ["Sizing is a paper estimate; actual fill prices, fees and margin tiers can differ."]
    liq = None
    if maintenance_margin_rate is not None and 0 <= maintenance_margin_rate < 1:
        # Simplified isolated linear-contract stress estimate, not the exchange's tiered liquidation engine.
        if side == "LONG":
            liq = entry * (1 - 1 / leverage) / (1 - maintenance_margin_rate)
        else:
            liq = entry * (1 + 1 / leverage) / (1 + maintenance_margin_rate)
        notes.append("Liquidation estimate is simplified and excludes tiered/cross-margin interactions.")
    return RiskPlan(risk_budget, contracts, qty, notional, estimated_loss, margin, liq, leverage, notes)
