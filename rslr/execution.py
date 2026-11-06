from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rslr.config import HARD_AUTO_TRADING_ALLOWED, HARD_LIVE_EXECUTION_ALLOWED, settings
from rslr.models import TradeSignal


class ExecutionLocked(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedOrder:
    symbol: str
    side: str
    quantity: float
    leverage: int
    entry_reference: float
    stop_loss: float
    take_profit: float
    reduce_only_exit: bool = True


class MEXCExecutionAdapter:
    """Order-preparation scaffold. Network order submission is always blocked.

    This is deliberately stronger than an environment flag: a deployment cannot place
    a live order through this adapter even if AUTO_TRADING_ENABLED is accidentally set.
    """

    def prepare(self, signal: TradeSignal) -> PreparedOrder:
        if signal.contracts <= 0 or signal.leverage <= 0:
            raise ValueError("Invalid order quantity or leverage")
        return PreparedOrder(
            symbol=signal.symbol,
            side="BUY" if signal.side == "LONG" else "SELL",
            quantity=signal.contracts,
            leverage=int(signal.leverage),
            entry_reference=signal.reference_entry,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )

    def submit_order(self, order: PreparedOrder) -> dict[str, Any]:
        del order
        if not HARD_LIVE_EXECUTION_ALLOWED or not HARD_AUTO_TRADING_ALLOWED:
            raise ExecutionLocked("LIVE ORDER BLOCKED: hard-coded RSLR execution safety lock is active")
        if not settings.auto_trading_config_value or not settings.live_execution_config_value:
            raise ExecutionLocked("LIVE ORDER BLOCKED: both environment flags must be explicitly enabled")
        # Intentionally unreachable: no private trading request is implemented in this build.
        raise ExecutionLocked("LIVE ORDER BLOCKED: exchange order transport is not enabled in this release")
