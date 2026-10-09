from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from ..database import Database
from .mexc_client import MexcClient
from .signal_validator import ValidatedSignal

LOGGER = logging.getLogger(__name__)


class PaperTrader:
    """Virtual-money execution for already validated V11 signals.

    This class only reads public MEXC market data. It never submits exchange
    orders and does not use the live executor. The virtual wallet/trade ledger
    is persisted in the existing SQLite database.
    """

    def __init__(self, db: Database, client: MexcClient, settings: Settings) -> None:
        self.db = db
        self.client = client
        self.settings = settings
        self.db.ensure_paper_wallet(float(settings.paper_initial_balance))
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._cycle_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.paper_trading_enabled)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _slipped_price(price: float, side: str, *, entry: bool, bps: float) -> float:
        fraction = max(0.0, float(bps)) / 10_000.0
        adverse_up = (side == "LONG" and entry) or (side == "SHORT" and not entry)
        return float(price) * (1.0 + fraction if adverse_up else 1.0 - fraction)

    def open_from_signal(self, signal: ValidatedSignal) -> dict[str, Any] | None:
        """Open one virtual position. Signal keys make repeated scans idempotent."""
        if not self.enabled:
            return None
        wallet = self.db.get_paper_wallet() or self.db.ensure_paper_wallet(
            float(self.settings.paper_initial_balance)
        )
        balance = float(wallet["balance"])
        margin_pct = max(0.0, float(self.settings.paper_margin_percent)) / 100.0
        leverage = int(self.settings.paper_leverage)
        if balance <= 0 or margin_pct <= 0 or leverage < 1:
            LOGGER.warning("PAPER trade rejected due to invalid wallet/risk configuration")
            return None

        margin = balance * margin_pct
        notional = margin * leverage
        side = str(signal.side).upper()
        raw_entry = float(signal.plan.entry)
        stop_loss = float(signal.plan.stop_loss)
        target = float(signal.plan.tp)
        entry = self._slipped_price(
            raw_entry, side, entry=True, bps=float(self.settings.paper_slippage_bps)
        )
        if not all(math.isfinite(x) and x > 0 for x in (entry, stop_loss, target)):
            LOGGER.warning("PAPER trade rejected: non-finite or non-positive trade levels")
            return None
        if (side == "LONG" and not stop_loss < entry < target) or (
            side == "SHORT" and not target < entry < stop_loss
        ):
            LOGGER.warning("PAPER trade rejected: slippage invalidated SL/TP geometry for %s", signal.symbol)
            return None

        quantity = notional / entry
        entry_fee = notional * max(0.0, float(self.settings.paper_fee_rate))
        result, trade = self.db.create_paper_trade(
            signal_key=str(signal.key),
            symbol=str(signal.symbol),
            side=side,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=target,
            leverage=leverage,
            margin=margin,
            notional=notional,
            quantity=quantity,
            entry_fee=entry_fee,
            max_open_trades=int(self.settings.paper_max_open_trades),
            opened_at=self._now(),
        )
        if result == "OPENED" and trade:
            LOGGER.info(
                "PAPER TRADE OPENED | id=%s | %s %s | entry=%.10g | margin=%.4f | notional=%.4f | leverage=%sx | SL=%.10g | TP=%.10g",
                trade["id"], side, signal.symbol, entry, margin, notional, leverage, stop_loss, target,
            )
            return trade
        if result not in {"DUPLICATE", "MAX_OPEN_TRADES", "INSUFFICIENT_BALANCE"}:
            LOGGER.warning("PAPER trade not opened: %s", result)
        else:
            LOGGER.info("PAPER trade skipped | symbol=%s | reason=%s", signal.symbol, result)
        return None

    async def start(self) -> None:
        if not self.enabled or (self._task and not self._task.done()):
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="paper-trading-monitor")
        LOGGER.info(
            "PAPER TRADING ENABLED | initial_balance=%.2f | margin=%.2f%% | leverage=%sx | live_orders=DISABLED",
            float(self.settings.paper_initial_balance), float(self.settings.paper_margin_percent),
            int(self.settings.paper_leverage),
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        LOGGER.info("Paper trading monitor stopped")

    async def monitor_once(self) -> None:
        """Mark open paper positions and close when an observed price reaches SL/TP."""
        if not self.enabled or self._cycle_lock.locked():
            return
        async with self._cycle_lock:
            trades = self.db.list_paper_trades(status="OPEN", limit=200)
            for trade in trades:
                try:
                    ticker = await self.client.get_ticker(str(trade["symbol"]))
                    price = self._extract_price(ticker)
                    if price is None:
                        LOGGER.warning("PAPER mark skipped: no usable last price for %s", trade["symbol"])
                        continue
                    self.db.update_paper_mark(int(trade["id"]), price)
                    reason = self._exit_reason(trade, price)
                    if reason:
                        self._close_trade(trade, reason, float(trade["stop_loss"] if reason == "SL" else trade["take_profit"]))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("Paper trade monitor failed for %s", trade.get("symbol"))

    async def _run(self) -> None:
        interval = max(5, int(self.settings.paper_poll_seconds))
        while not self._stopping.is_set():
            await self.monitor_once()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _extract_price(ticker: dict[str, Any]) -> float | None:
        if not isinstance(ticker, dict):
            return None
        for key in ("lastPrice", "last_price", "price", "fairPrice", "fair_price"):
            try:
                value = float(ticker.get(key))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value) and value > 0:
                return value
        return None

    @staticmethod
    def _exit_reason(trade: dict[str, Any], price: float) -> str | None:
        if str(trade["side"]).upper() == "LONG":
            if price <= float(trade["stop_loss"]):
                return "SL"
            if price >= float(trade["take_profit"]):
                return "TP"
        else:
            if price >= float(trade["stop_loss"]):
                return "SL"
            if price <= float(trade["take_profit"]):
                return "TP"
        return None

    def _close_trade(self, trade: dict[str, Any], reason: str, reference_price: float) -> dict[str, Any] | None:
        side = str(trade["side"]).upper()
        exit_price = self._slipped_price(
            reference_price, side, entry=False, bps=float(self.settings.paper_slippage_bps)
        )
        quantity = float(trade["quantity"])
        direction = 1.0 if side == "LONG" else -1.0
        gross_pnl = (exit_price - float(trade["entry_price"])) * quantity * direction
        exit_fee = abs(exit_price * quantity) * max(0.0, float(self.settings.paper_fee_rate))
        entry_fee = float(trade["entry_fee"])
        net_pnl = gross_pnl - entry_fee - exit_fee
        closed = self.db.close_paper_trade(
            trade_id=int(trade["id"]),
            exit_price=exit_price,
            exit_reason=reason,
            gross_pnl=gross_pnl,
            exit_fee=exit_fee,
            net_pnl=net_pnl,
            closed_at=self._now(),
        )
        if closed:
            LOGGER.info(
                "PAPER TRADE CLOSED | id=%s | %s %s | exit_reason=%s | net_pnl=%.6f USDT | fees=%.6f USDT",
                trade["id"], side, trade["symbol"], reason, net_pnl, entry_fee + exit_fee,
            )
        return closed

    def get_status(self) -> dict[str, Any]:
        wallet = self.db.get_paper_wallet() or self.db.ensure_paper_wallet(
            float(self.settings.paper_initial_balance)
        )
        opened = self.db.list_paper_trades(status="OPEN", limit=200)
        closed = self.db.list_paper_trades(status="CLOSED", limit=200)
        open_margin = sum(float(t["margin"]) for t in opened)
        unrealized = 0.0
        for trade in opened:
            direction = 1.0 if trade["side"] == "LONG" else -1.0
            gross = (float(trade["mark_price"]) - float(trade["entry_price"])) * float(trade["quantity"]) * direction
            expected_exit_fee = abs(float(trade["mark_price"]) * float(trade["quantity"])) * max(0.0, float(self.settings.paper_fee_rate))
            unrealized += gross - expected_exit_fee
        balance = float(wallet["balance"])
        return {
            "enabled": self.enabled,
            "initial_balance": float(wallet["initial_balance"]),
            "balance": balance,
            "open_margin": open_margin,
            "available_margin": max(0.0, balance - open_margin),
            "unrealized_pnl": unrealized,
            "equity": balance + unrealized,
            "realized_pnl": self.db.get_paper_realized_pnl(),
            "open_trades": opened,
            "closed_trades": closed,
            "leverage": int(self.settings.paper_leverage),
            "margin_percent": float(self.settings.paper_margin_percent),
        }
