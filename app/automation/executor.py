from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from .mexc_client import MexcClient
from .risk_manager import calculate_contract_quantity
from .signal_validator import ValidatedSignal
from .universe import ContractMeta

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    executed: bool
    order_id: str | None
    message: str


def build_limit_order_payload(
    signal: ValidatedSignal,
    meta: ContractMeta,
    *,
    risk_amount_usdt: float,
    leverage: int,
    open_type: int,
) -> dict[str, Any]:
    quantity = calculate_contract_quantity(
        risk_amount_usdt=risk_amount_usdt,
        entry=signal.plan.entry,
        stop_loss=signal.plan.stop_loss,
        contract_size=meta.contract_size,
        vol_unit=meta.vol_unit,
        min_vol=meta.min_vol,
        max_vol=meta.max_vol,
    )
    return {
        "symbol": signal.symbol,
        "price": signal.plan.entry,
        "vol": quantity,
        "leverage": int(leverage),
        "side": 1 if signal.side == "LONG" else 3,
        "type": 1,
        "openType": int(open_type),
        "externalOid": signal.key[:32],
        "positionMode": 1,
    }


class MexcExecutor:
    """Future execution adapter with a hard live-trading stop for this build.

    MEXC currently supports Futures API order placement, but the API has no
    sandbox environment. This project therefore ships the execution payload
    builder and API client without allowing a real order to be sent yet. The
    order path must be completed with post-fill position reconciliation and
    verified protective-order installation before the live gate is removed.
    """

    LIVE_IMPLEMENTED = False

    def __init__(self, client: MexcClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def execute(self, signal: ValidatedSignal, meta: ContractMeta) -> ExecutionResult:
        if not self.settings.auto_trade_enabled:
            return ExecutionResult(False, None, "AUTO_TRADE_ENABLED=false")
        if not self.settings.allow_live_execution:
            return ExecutionResult(False, None, "ALLOW_LIVE_EXECUTION=false")
        if not self.LIVE_IMPLEMENTED:
            return ExecutionResult(
                False,
                None,
                "Live execution is intentionally disabled until post-fill TP/SL reconciliation is completed",
            )

        # Kept unreachable until LIVE_IMPLEMENTED is deliberately changed.
        assets = await self.client.get_account_assets()
        usdt = next(
            (item for item in assets if str(item.get("currency", "")).upper() == "USDT"),
            None,
        )
        if not usdt:
            return ExecutionResult(False, None, "USDT Futures account asset was not returned")
        available = float(usdt.get("availableOpen", usdt.get("availableBalance", 0)) or 0)
        risk_amount = available * min(max(self.settings.max_risk_per_trade, 0.0), 0.05)
        payload = build_limit_order_payload(
            signal,
            meta,
            risk_amount_usdt=risk_amount,
            leverage=self.settings.mexc_default_leverage,
            open_type=self.settings.mexc_open_type,
        )
        order = await self.client.place_order(payload)
        LOGGER.warning("MEXC live order placed: order_id=%s", order.order_id)
        return ExecutionResult(True, order.order_id, "Order accepted")
