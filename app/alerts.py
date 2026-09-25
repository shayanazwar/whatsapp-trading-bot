from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .config import Settings
from .database import Alert, Database
from .market import MarketData, MarketRef

LOGGER = logging.getLogger(__name__)


@dataclass
class RuntimeState:
    last_price: float | None = None


class AlertEngine:
    def __init__(
        self,
        db: Database,
        market: MarketData,
        settings: Settings,
        on_trigger: Callable[[Alert, float], Awaitable[None]],
    ) -> None:
        self.db = db
        self.market = market
        self.settings = settings
        self.on_trigger = on_trigger
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._states: dict[int, RuntimeState] = {}

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="alert-engine")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                alerts = self._all_active_alerts()
                active_ids = {a.id for a in alerts}
                for alert_id in set(self._states) - active_ids:
                    self._states.pop(alert_id, None)
                for alert in alerts:
                    if alert.exchange != "binance":
                        continue
                    price = await self.market.binance_price(alert.symbol)
                    if price is None:
                        continue
                    state = self._states.setdefault(alert.id, RuntimeState())
                    if state.last_price is None:
                        state.last_price = price
                        continue
                    crossed = (
                        alert.condition == "above"
                        and state.last_price < alert.target <= price
                    ) or (
                        alert.condition == "below"
                        and state.last_price > alert.target >= price
                    )
                    state.last_price = price
                    if crossed:
                        if self.db.deactivate(alert.id, alert.phone):
                            await self.on_trigger(alert, price)
                            self._states.pop(alert.id, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Alert engine loop failed")
            await asyncio.sleep(max(0.25, self.settings.alert_check_seconds))

    def _all_active_alerts(self) -> list[Alert]:
        # The database is small for a personal bot. This keeps the implementation deterministic.
        # Pull from SQLite by reading all rows for a phone is not possible without a list of phones,
        # so use a direct helper connection/query here.
        import sqlite3

        with sqlite3.connect(self.db.path) as conn:
            rows = conn.execute(
                "SELECT id, phone, exchange, symbol, condition, target, active, created_at FROM alerts WHERE active = 1"
            ).fetchall()
        return [
            Alert(
                id=int(row[0]),
                phone=str(row[1]),
                exchange=str(row[2]),
                symbol=str(row[3]),
                condition=str(row[4]),
                target=float(row[5]),
                active=bool(row[6]),
                created_at=str(row[7]),
            )
            for row in rows
        ]
