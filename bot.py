from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from .charts import ChartRenderer
from .config import Settings
from .database import Alert, Database
from .market import MarketData, MarketRef, TIMEFRAME_ALIASES
from .whatsapp import WhatsAppClient

LOGGER = logging.getLogger(__name__)

HELP = """📈 WhatsApp Trading Bot

Commands:

PRICE BTCUSDT
CHART BTCUSDT 1H
CHART BINANCE:BTCUSDT 4H
CHART BYBIT:BTC/USDT 15M

ALERT BTCUSDT ABOVE 120000
ALERT BTCUSDT BELOW 110000
ALERTS
DELETE 12
DELETE ALL

SEARCH PEPE
SEARCH AIOT

Charts: 5M, 15M, 1H, 4H, 1D

Alerts are one-shot and trigger on a price crossing the target.
"""

COMMAND_RE = re.compile(r"^/?([A-Z]+)\b(.*)$", re.IGNORECASE | re.DOTALL)


def normalize_symbol_token(value: str) -> str:
    return value.strip().upper()


def fmt_price(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if abs(value) >= 0.01:
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return f"{value:.10f}".rstrip("0").rstrip(".")


class Bot:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        market: MarketData,
        whatsapp: WhatsAppClient,
        charts: ChartRenderer,
    ) -> None:
        self.settings = settings
        self.db = db
        self.market = market
        self.whatsapp = whatsapp
        self.charts = charts

    async def handle(self, phone: str, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        if self.settings.allowed_user_set and phone not in self.settings.allowed_user_set:
            await self.whatsapp.send_text(phone, "⛔ This bot is private.")
            return

        match = COMMAND_RE.match(cleaned)
        if not match:
            await self.whatsapp.send_text(phone, HELP)
            return
        command = match.group(1).upper()
        args = match.group(2).strip()

        try:
            if command in {"HELP", "MENU", "START"}:
                await self.whatsapp.send_text(phone, HELP)
            elif command == "PRICE":
                await self._price(phone, args)
            elif command == "CHART":
                await self._chart(phone, args)
            elif command == "ALERT":
                await self._create_alert(phone, args)
            elif command == "ALERTS":
                await self._list_alerts(phone)
            elif command == "DELETE":
                await self._delete(phone, args)
            elif command == "SEARCH":
                await self._search(phone, args)
            else:
                # Friendly shortcut: "BTCUSDT 1H" means chart, and "BTCUSDT" means price.
                await self._shortcut(phone, cleaned)
        except Exception as exc:
            LOGGER.exception("Command failed: %s", cleaned)
            await self.whatsapp.send_text(phone, f"❌ {exc}")

    async def _price(self, phone: str, args: str) -> None:
        if not args:
            raise ValueError("Usage: PRICE BTCUSDT")
        raw = args.split()[0]
        ref = await self.market.resolve(raw)
        if ref.exchange != "binance":
            raise ValueError("PRICE currently uses Binance spot real-time prices. Use a Binance symbol.")
        price = await self.market.binance_price(ref.symbol)
        if price is None:
            raise ValueError("Could not get the current price.")
        await self.whatsapp.send_text(
            phone,
            f"💰 {ref.symbol}\nExchange: BINANCE SPOT\nPrice: ${fmt_price(price)}",
        )

    async def _chart(self, phone: str, args: str) -> None:
        parts = args.split()
        if len(parts) < 2:
            raise ValueError("Usage: CHART BTCUSDT 1H")
        raw_symbol, raw_tf = parts[0], parts[1]
        tf = TIMEFRAME_ALIASES.get(raw_tf.upper())
        if not tf:
            raise ValueError("Supported chart timeframes: 5M, 15M, 1H, 4H, 1D")
        ref = await self.market.resolve(raw_symbol)
        rows = await self.market.ohlcv(ref, tf, self.settings.chart_default_bars)
        path = await self.charts.render(ref, tf, rows)
        try:
            media_id = await self.whatsapp.upload_image(path)
            await self.whatsapp.send_image(
                phone,
                media_id,
                caption=f"📊 {ref.exchange.upper()} {ref.symbol} • {tf.upper()}\nEMA 21 / EMA 50",
            )
        finally:
            Path(path).unlink(missing_ok=True)

    async def _create_alert(self, phone: str, args: str) -> None:
        parts = args.split()
        if len(parts) != 3:
            raise ValueError("Usage: ALERT BTCUSDT ABOVE 120000")
        raw_symbol, condition, target_raw = parts
        condition = condition.lower()
        if condition not in {"above", "below"}:
            raise ValueError("Condition must be ABOVE or BELOW")
        try:
            target = float(target_raw.replace(",", ""))
        except ValueError as exc:
            raise ValueError("Target must be a number.") from exc
        if target <= 0:
            raise ValueError("Target must be greater than zero.")
        ref = await self.market.resolve(raw_symbol)
        if ref.exchange != "binance":
            raise ValueError("Real-time alerts in this version are enabled for Binance spot symbols.")
        current = await self.market.binance_price(ref.symbol)
        if current is None:
            raise ValueError("Could not read the current Binance price. Try again.")
        if condition == "above" and current >= target:
            raise ValueError(f"Current price ${fmt_price(current)} is already at/above ${fmt_price(target)}.")
        if condition == "below" and current <= target:
            raise ValueError(f"Current price ${fmt_price(current)} is already at/below ${fmt_price(target)}.")
        alert = self.db.create_alert(phone, "binance", ref.symbol, condition, target)
        await self.whatsapp.send_text(
            phone,
            f"✅ Alert #{alert.id} created\n\n{ref.symbol}\nCondition: {condition.upper()} ${fmt_price(target)}\nCurrent: ${fmt_price(current)}\n\nYou will receive one WhatsApp alert when price crosses the target.",
        )

    async def _list_alerts(self, phone: str) -> None:
        alerts = self.db.list_alerts(phone)
        if not alerts:
            await self.whatsapp.send_text(phone, "🔔 No active alerts.")
            return
        lines = ["🔔 ACTIVE ALERTS", ""]
        for alert in alerts:
            lines.append(f"#{alert.id} {alert.symbol} {alert.condition.upper()} ${fmt_price(alert.target)}")
        lines.append("")
        lines.append("DELETE <id> to remove an alert.")
        await self.whatsapp.send_text(phone, "\n".join(lines))

    async def _delete(self, phone: str, args: str) -> None:
        if not args:
            raise ValueError("Usage: DELETE 12 or DELETE ALL")
        token = args.strip().upper()
        if token == "ALL":
            count = self.db.deactivate_all(phone)
            await self.whatsapp.send_text(phone, f"🗑️ Deleted {count} active alert(s).")
            return
        try:
            alert_id = int(token)
        except ValueError as exc:
            raise ValueError("Alert id must be a number or ALL.") from exc
        if self.db.deactivate(alert_id, phone):
            await self.whatsapp.send_text(phone, f"🗑️ Alert #{alert_id} deleted.")
        else:
            await self.whatsapp.send_text(phone, f"❌ Active alert #{alert_id} was not found.")

    async def _search(self, phone: str, args: str) -> None:
        if not args:
            raise ValueError("Usage: SEARCH PEPE")
        results = await self.market.search(args, limit=20)
        if not results:
            await self.whatsapp.send_text(phone, "No matching spot markets found.")
            return
        lines = [f"🔎 Matches for {args.upper()}:", ""]
        for ref in results:
            lines.append(f"{ref.exchange.upper()}: {ref.symbol}")
        lines.append("")
        lines.append("For a chart, use: CHART EXCHANGE:SYMBOL 1H")
        await self.whatsapp.send_text(phone, "\n".join(lines))

    async def _shortcut(self, phone: str, text: str) -> None:
        parts = text.split()
        if len(parts) == 2 and parts[1].upper() in TIMEFRAME_ALIASES:
            await self._chart(phone, text)
        elif len(parts) == 1:
            await self._price(phone, text)
        else:
            await self.whatsapp.send_text(phone, HELP)

    async def send_triggered_alert(self, alert: Alert, price: float) -> None:
        body = (
            f"🚨 PRICE ALERT\n\n"
            f"{alert.symbol}\n"
            f"BINANCE SPOT\n"
            f"Current: ${fmt_price(price)}\n"
            f"Target: ${fmt_price(alert.target)}\n"
            f"Condition: {alert.condition.upper()}\n\n"
            f"Alert #{alert.id} is now completed."
        )
        await self.whatsapp.send_text(alert.phone, body)
