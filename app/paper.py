"""Persistent local paper-trading broker; never sends an exchange order."""
from __future__ import annotations

from typing import Any, Mapping

from app.storage import Store


class PaperBroker:
    def __init__(self, store: Store) -> None:
        self.store = store

    def open_position(self, *, symbol: str, side: str, entry_time: int, entry_price: float,
                      stop_price: float, target_price: float, quantity: float,
                      candidate_id: str, meta: Mapping[str, Any] | None = None) -> str:
        if side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        if min(entry_price, stop_price, target_price, quantity) <= 0:
            raise ValueError("Paper position prices and quantity must be positive")
        if side == "LONG" and not stop_price < entry_price < target_price:
            raise ValueError("Long paper position requires stop < entry < target")
        if side == "SHORT" and not target_price < entry_price < stop_price:
            raise ValueError("Short paper position requires target < entry < stop")
        opened = self.store.open_paper_position({
            "symbol": symbol, "side": side, "entry_time": entry_time, "entry_price": entry_price,
            "stop_price": stop_price, "target_price": target_price, "quantity": quantity,
            "mode": "paper_simulated", "meta": {"candidate_id": candidate_id, **dict(meta or {})},
        })
        self.store.append_event("PAPER_POSITION_OPENED_SIMULATED", {
            "position_id": opened, "symbol": symbol, "side": side, "entry_time": entry_time,
            "entry_price": entry_price, "stop_price": stop_price, "target_price": target_price,
            "quantity": quantity, "candidate_id": candidate_id, "simulated": True,
        }, symbol=symbol, idempotency_key=f"paper-open:{candidate_id}")
        return opened

    def close_position(self, position_id: str, *, exit_time: int, exit_price: float,
                       close_reason: str, realised_pnl: float) -> bool:
        if exit_price <= 0:
            raise ValueError("exit_price must be positive")
        return self.store.close_paper_position(position_id, exit_time=exit_time, exit_price=exit_price,
                                               realised_pnl=realised_pnl, close_reason=close_reason)

    def open_positions(self) -> list[dict[str, Any]]:
        return self.store.paper_positions(open_only=True)
