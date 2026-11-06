from __future__ import annotations

import math
from dataclasses import dataclass

from rslr.models import Contract


@dataclass(frozen=True)
class SizingResult:
    accepted: bool
    reason: str
    contracts: float = 0.0
    risk_amount_usd: float = 0.0
    risk_pct: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    leverage: float = 0.0
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    funding_usd: float = 0.0
    gross_rr: float = 0.0
    net_rr: float = 0.0
    margin_required_usd: float = 0.0


def round_down(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("Quantity step must be positive")
    return math.floor((value + 1e-12) / step) * step


def size_position(
    *,
    side: str,
    entry: float,
    stop: float,
    equity: float,
    contract: Contract,
    risk_fraction: float = 0.005,
    taker_fee_rate: float = 0.0008,
    slippage_rate: float = 0.0005,
    funding_rate: float = 0.0,
    funding_cycles_estimate: float = 1.5,
    leverage: int = 2,
    max_leverage: int = 5,
    max_margin_fraction: float = 0.25,
) -> SizingResult:
    if side not in {"LONG", "SHORT"} or entry <= 0 or equity <= 0:
        return SizingResult(False, "Invalid side, price or equity")
    if contract.contract_size <= 0 or contract.quantity_step <= 0 or contract.min_volume <= 0:
        return SizingResult(False, "Invalid exchange contract specifications")
    tick = contract.price_tick
    if tick > 0:
        # Round protective stops away from entry so the modeled risk is not understated.
        stop = math.floor(stop / tick + 1e-12) * tick if side == "LONG" else math.ceil(stop / tick - 1e-12) * tick
    if (side == "LONG" and not stop < entry) or (side == "SHORT" and not stop > entry):
        return SizingResult(False, "Stop-loss is on the wrong side of entry after tick rounding")
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return SizingResult(False, "Zero stop distance")
    lev = min(max(1, int(leverage)), max(1, int(max_leverage)), max(1, int(contract.max_leverage)))
    if lev > max_leverage:
        return SizingResult(False, "Leverage exceeds configured maximum")

    notional_per_contract = entry * contract.contract_size
    round_trip_fee = 2 * max(taker_fee_rate, contract.taker_fee_rate) * notional_per_contract
    round_trip_slippage = 2 * slippage_rate * notional_per_contract
    funding_cost_per_contract = abs(funding_rate) * max(0.0, funding_cycles_estimate) * notional_per_contract
    planned_loss_per_contract = stop_distance * contract.contract_size + round_trip_fee + round_trip_slippage + funding_cost_per_contract
    budget = equity * risk_fraction
    if planned_loss_per_contract <= 0 or budget <= 0:
        return SizingResult(False, "Invalid risk calculation")

    raw_qty = budget / planned_loss_per_contract
    max_notional = equity * max_margin_fraction * lev
    max_qty_by_margin = max_notional / notional_per_contract
    quantity = round_down(min(raw_qty, max_qty_by_margin), contract.quantity_step)
    if quantity < contract.min_volume:
        return SizingResult(False, "Minimum exchange quantity exceeds risk or margin budget")

    exposure = quantity * contract_size_value(contract)
    gross_profit = stop_distance * exposure
    fees = quantity * round_trip_fee
    slippage = quantity * round_trip_slippage
    funding = quantity * funding_cost_per_contract
    net_loss = gross_profit + fees + slippage + funding
    net_profit = gross_profit - fees - slippage - funding
    if net_profit <= 0:
        return SizingResult(False, "Estimated costs consume the entire 1R reward")
    margin = quantity * notional_per_contract / lev
    if margin > equity * max_margin_fraction + 1e-8:
        return SizingResult(False, "Margin cap exceeded after rounding")
    tp = entry + stop_distance if side == "LONG" else entry - stop_distance
    if tick > 0:
        tp = math.ceil(tp / tick - 1e-12) * tick if side == "LONG" else math.floor(tp / tick + 1e-12) * tick
    risk_used = net_loss
    return SizingResult(
        accepted=True,
        reason="accepted",
        contracts=quantity,
        risk_amount_usd=risk_used,
        risk_pct=risk_used / equity,
        stop_loss=stop,
        take_profit=tp,
        leverage=float(lev),
        fees_usd=fees,
        slippage_usd=slippage,
        funding_usd=funding,
        gross_rr=1.0,
        net_rr=net_profit / net_loss if net_loss else 0.0,
        margin_required_usd=margin,
    )


def contract_size_value(contract: Contract) -> float:
    return contract.contract_size
