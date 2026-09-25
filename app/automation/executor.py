from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from .mexc_client import MexcClient
from .risk_manager import (
    calculate_contract_quantity,
    calculate_risk_amount,
    validate_atr_stop_distance,
    validate_levels,
)
from .signal_validator import ValidatedSignal
from .universe import ContractMeta

LOGGER = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# HARD AUTO-TRADE RISK LIMIT
# ----------------------------------------------------------------------

MAX_AUTO_TRADE_RISK_PERCENT = 1.0


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
    """
    Build a MEXC Futures opening limit order.

    Position size is calculated from:
        allowed risk / SL distance

    Leverage does NOT increase the allowed risk.
    """

    if risk_amount_usdt <= 0:
        raise ValueError(
            "Risk amount must be greater than zero"
        )

    if leverage <= 0:
        raise ValueError(
            "Leverage must be greater than zero"
        )

    quantity = calculate_contract_quantity(
        risk_amount_usdt=risk_amount_usdt,
        entry=signal.plan.entry,
        stop_loss=signal.plan.stop_loss,
        contract_size=meta.contract_size,
        vol_unit=meta.vol_unit,
        min_vol=meta.min_vol,
        max_vol=meta.max_vol,
    )

    if quantity <= 0:
        raise ValueError(
            "Calculated order quantity is zero"
        )

    return {
        "symbol": signal.symbol,
        "price": signal.plan.entry,
        "vol": quantity,
        "leverage": int(leverage),

        # MEXC Futures:
        # 1 = open long
        # 3 = open short
        "side": (
            1
            if signal.side == "LONG"
            else 3
        ),

        # 1 = limit order
        "type": 1,

        "openType": int(open_type),

        # Unique client-side identifier.
        "externalOid": signal.key[:32],

        # Existing project uses position mode 1.
        "positionMode": 1,
    }


class MexcExecutor:
    """
    MEXC Futures execution adapter.

    IMPORTANT:
    Live execution remains deliberately disabled.

    Future live execution requirements:
      1. Futures equity retrieved from MEXC.
      2. Maximum risk = 1% of total Futures equity.
      3. Position size calculated from Entry -> SL.
      4. Contract limits validated.
      5. Order submitted.
      6. Actual fill reconciled.
      7. Protective SL installed and verified.
      8. TP1/TP2 installed and verified.
      9. Position monitored.
     10. Emergency recovery available.

    No real order is allowed until all required
    execution protections are implemented.
    """

    LIVE_IMPLEMENTED = False

    def __init__(
        self,
        client: MexcClient,
        settings: Settings,
    ) -> None:
        self.client = client
        self.settings = settings

    async def execute(
        self,
        signal: ValidatedSignal,
        meta: ContractMeta,
    ) -> ExecutionResult:

        # ==============================================================
        # ABSOLUTE SAFETY GATES
        # ==============================================================

        if not self.settings.auto_trade_enabled:
            return ExecutionResult(
                False,
                None,
                "AUTO_TRADE_ENABLED=false",
            )

        if not self.settings.allow_live_execution:
            return ExecutionResult(
                False,
                None,
                "ALLOW_LIVE_EXECUTION=false",
            )

        if not self.LIVE_IMPLEMENTED:
            return ExecutionResult(
                False,
                None,
                (
                    "Live execution is intentionally disabled "
                    "until post-fill position reconciliation, "
                    "protective SL/TP verification, and emergency "
                    "recovery are completed"
                ),
            )

        # ==============================================================
        # SIGNAL VALIDATION
        # ==============================================================

        valid, reason = validate_levels(
            signal.plan,
            min_rr=2.0,
        )

        if not valid:
            return ExecutionResult(
                False,
                None,
                f"Execution rejected: {reason}",
            )

        if signal.side not in {
            "LONG",
            "SHORT",
        }:
            return ExecutionResult(
                False,
                None,
                "Execution rejected: invalid trade side",
            )

        # ==============================================================
        # FUTURES EQUITY
        # ==============================================================

        try:
            futures_equity = (
                await self.client.get_futures_equity()
            )

        except Exception as exc:
            LOGGER.exception(
                "Could not retrieve MEXC Futures equity"
            )

            return ExecutionResult(
                False,
                None,
                f"Could not retrieve Futures equity: {exc}",
            )

        if futures_equity <= 0:
            return ExecutionResult(
                False,
                None,
                "Execution rejected: Futures equity <= 0",
            )

        # ==============================================================
        # HARD 1% RISK CAP
        # ==============================================================

        # Never allow the settings value to exceed 1%.
        configured_risk_percent = float(
            getattr(
                self.settings,
                "max_risk_per_trade",
                1.0,
            )
            or 1.0
        )

        risk_percent = min(
            max(configured_risk_percent, 0.0),
            MAX_AUTO_TRADE_RISK_PERCENT,
        )

        if risk_percent <= 0:
            return ExecutionResult(
                False,
                None,
                "Execution rejected: risk percentage <= 0",
            )

        risk_amount = calculate_risk_amount(
            balance_usdt=futures_equity,
            risk_pct=risk_percent,
        )

        # Absolute safety check.
        absolute_risk_cap = (
            futures_equity
            * MAX_AUTO_TRADE_RISK_PERCENT
            / 100.0
        )

        if risk_amount > absolute_risk_cap:
            LOGGER.error(
                "RISK SAFETY FAILURE: calculated risk "
                "%.8f exceeds hard cap %.8f",
                risk_amount,
                absolute_risk_cap,
            )

            return ExecutionResult(
                False,
                None,
                "Execution rejected: risk exceeds hard 1% cap",
            )

        # ==============================================================
        # CONTRACT SIZE / POSITION SIZE
        # ==============================================================

        try:
            quantity = calculate_contract_quantity(
                risk_amount_usdt=risk_amount,
                entry=signal.plan.entry,
                stop_loss=signal.plan.stop_loss,
                contract_size=meta.contract_size,
                vol_unit=meta.vol_unit,
                min_vol=meta.min_vol,
                max_vol=meta.max_vol,
            )

        except Exception as exc:
            LOGGER.warning(
                "Position sizing rejected for %s: %s",
                signal.symbol,
                exc,
            )

            return ExecutionResult(
                False,
                None,
                f"Position sizing rejected: {exc}",
            )

        if quantity <= 0:
            return ExecutionResult(
                False,
                None,
                "Execution rejected: quantity <= 0",
            )

        # ==============================================================
        # RE-CALCULATE ACTUAL MAXIMUM SL LOSS
        # ==============================================================
        #
        # This is deliberately checked again after MEXC contract
        # quantization. If the minimum contract size would make the
        # actual risk exceed 1%, the trade is rejected.
        #

        actual_sl_risk = (
            abs(
                signal.plan.entry
                - signal.plan.stop_loss
            )
            * meta.contract_size
            * quantity
        )

        if actual_sl_risk > absolute_risk_cap:
            LOGGER.warning(
                "Execution rejected: actual SL risk "
                "%.8f > hard 1%% cap %.8f",
                actual_sl_risk,
                absolute_risk_cap,
            )

            return ExecutionResult(
                False,
                None,
                (
                    "Execution rejected: minimum contract "
                    "size would exceed the 1% risk cap"
                ),
            )

        # ==============================================================
        # OPTIONAL ATR SAFETY CHECK
        # ==============================================================

        atr = signal.analysis.get("atr")

        if atr is not None:
            try:
                atr_value = float(atr)

                if atr_value > 0:
                    atr_ok, atr_reason = (
                        validate_atr_stop_distance(
                            entry=signal.plan.entry,
                            stop_loss=signal.plan.stop_loss,
                            atr=atr_value,
                        )
                    )

                    if not atr_ok:
                        return ExecutionResult(
                            False,
                            None,
                            (
                                "Execution rejected: "
                                f"{atr_reason}"
                            ),
                        )

            except (
                TypeError,
                ValueError,
            ):
                return ExecutionResult(
                    False,
                    None,
                    "Execution rejected: invalid ATR",
                )

        # ==============================================================
        # BUILD ORDER
        # ==============================================================

        try:
            payload = build_limit_order_payload(
                signal,
                meta,
                risk_amount_usdt=risk_amount,
                leverage=self.settings.mexc_default_leverage,
                open_type=self.settings.mexc_open_type,
            )

        except Exception as exc:
            LOGGER.warning(
                "Could not build MEXC order: %s",
                exc,
            )

            return ExecutionResult(
                False,
                None,
                f"Order construction rejected: {exc}",
            )

        # Make absolutely sure the payload quantity matches the
        # independently checked quantity.
        payload["vol"] = quantity

        LOGGER.warning(
            "LIVE execution gate reached for %s %s: "
            "equity=%.8f risk=%.8f (%.2f%%) "
            "quantity=%.8f entry=%.8f SL=%.8f",
            signal.symbol,
            signal.side,
            futures_equity,
            actual_sl_risk,
            (
                actual_sl_risk
                / futures_equity
                * 100.0
            ),
            quantity,
            signal.plan.entry,
            signal.plan.stop_loss,
        )

        # ==============================================================
        # FINAL LIVE ORDER GATE
        # ==============================================================

        # This remains unreachable because LIVE_IMPLEMENTED=False.
        #
        # When live execution is eventually implemented, this section
        # MUST be followed immediately by:
        #
        #   order fill reconciliation
        #   protective SL installation
        #   TP1/TP2 installation
        #   protection verification
        #
        # before the position is considered ACTIVE.

        order = await self.client.place_order(
            payload
        )

        LOGGER.warning(
            "MEXC live order placed: "
            "order_id=%s",
            order.order_id,
        )

        return ExecutionResult(
            True,
            order.order_id,
            "Order accepted",
        )
