from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from app.models import ContractInfo


def _d(value: float | str) -> Decimal:
    return Decimal(str(value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def calculate_quantity(price: float, contract: ContractInfo, max_margin: float, leverage: float) -> dict[str, float | bool]:
    if price <= 0 or max_margin <= 0 or leverage <= 0:
        raise ValueError("price, margin and leverage must be positive")
    contract_size = _d(contract.contract_size)
    raw_contracts = (_d(max_margin) * _d(leverage)) / (contract_size * _d(price))
    qty = floor_to_step(raw_contracts, _d(contract.volume_step))
    if qty < _d(contract.min_volume):
        return {"valid": False, "contracts": 0.0, "notional": 0.0, "margin": 0.0}
    if qty > _d(contract.max_volume):
        qty = floor_to_step(_d(contract.max_volume), _d(contract.volume_step))
    notional = qty * contract_size * _d(price)
    margin = notional / _d(leverage)
    return {"valid": True, "contracts": float(qty), "notional": float(notional), "margin": float(margin)}


def round_price_to_tick(price: float, tick: float, direction: str) -> float:
    p, t = _d(price), _d(tick)
    units = p / t
    if direction == "LONG":
        rounded = units.to_integral_value(rounding="ROUND_CEILING") * t
    else:
        rounded = units.to_integral_value(rounding=ROUND_DOWN) * t
    return float(rounded)
