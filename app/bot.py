from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

try:
    from .charts import ChartRenderer
    from .formatting import fmt_price
    from .analysis.engine import analyze_symbol
    from .backtest.runner import BacktestAlreadyRunning, BacktestRunner
    from .config import Settings
    from .database import Alert, Database
    from .market import MarketData, MarketRef, TIMEFRAME_ALIASES
    from .whatsapp import WhatsAppClient
    from .telegram import TelegramClient
except ImportError:
    from charts import ChartRenderer
    from formatting import fmt_price
    from app.analysis.engine import analyze_symbol
    from app.backtest.runner import BacktestAlreadyRunning, BacktestRunner
    from config import Settings
    from database import Alert, Database
    from market import MarketData, MarketRef, TIMEFRAME_ALIASES
    from whatsapp import WhatsAppClient
    from telegram import TelegramClient

LOGGER = logging.getLogger(__name__)

HELP = """📈 Pak Trading Academy Trading Bot

💰 PRICE
PRICE BTCUSDT

📊 ANALYZE
ANALYZE BTCUSDT

📈 CHART
CHART BTCUSDT 1H

🔔 ALERT
ALERT BTCUSDT ABOVE 120000
ALERTS • DELETE 12 • DELETE ALL

🔎 SEARCH
SEARCH PEPE

🧪 BACKTEST
BACKTEST 1D
BACKTEST 7D

⏱ SIGNAL: 1D • 12H • 4H • 1H
CHART: MEXC-supported timeframes
"""

COMMAND_RE = re.compile(r"^/?([A-Z]+)\b(.*)$", re.IGNORECASE | re.DOTALL)


def normalize_symbol_token(value: str) -> str:
    return value.strip().upper()




class Bot:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        market: MarketData,
        whatsapp: WhatsAppClient,
        charts: ChartRenderer,
        telegram: TelegramClient | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.market = market
        self.whatsapp = whatsapp
        self.telegram = telegram
        self.charts = charts
        self.backtest_runner: BacktestRunner | None = None
        self._last_inbound_phone: str | None = None

    @staticmethod
    def _is_telegram_target(target: str) -> bool:
        return target.startswith("tg:") or target.startswith("telegram:")

    async def _send_text(self, target: str, body: str):
        if self._is_telegram_target(target):
            if self.telegram is None:
                raise RuntimeError("Telegram channel is not configured.")
            return await self.telegram.send_text(target, body)
        return await self.whatsapp.send_text(target, body)

    async def _send_image(self, target: str, path: Path, caption: str | None = None):
        if self._is_telegram_target(target):
            if self.telegram is None:
                raise RuntimeError("Telegram channel is not configured.")
            return await self.telegram.send_image(target, path, caption)

        media_id = await self.whatsapp.upload_image(path)
        return await self.whatsapp.send_image(target, media_id, caption)

    def set_backtest_runner(self, runner: BacktestRunner) -> None:
        self.backtest_runner = runner
    async def handle(self, phone: str, text: str) -> None:
        cleaned = text.strip()

        if not cleaned:
            return

        if self._is_telegram_target(phone):
            allowed_targets = self.settings.telegram_allowed_user_set
        else:
            allowed_targets = self.settings.allowed_user_set

        if (
            allowed_targets
            and phone not in allowed_targets
        ):
            await self._send_text(
                phone,
                "⛔ This bot is private.",
            )
            return

        self._last_inbound_phone = phone

        match = COMMAND_RE.match(cleaned)

        if not match:
            await self._send_text(
                phone,
                HELP,
            )
            return

        command = match.group(1).upper()
        args = match.group(2).strip()

        try:
            if command in {"HELP", "MENU", "START"}:
                await self._send_text(
                    phone,
                    HELP,
                )

            elif command == "PRICE":
                await self._price(
                    phone,
                    args,
                )

            elif command == "ANALYZE":
                await self._analyze(
                    phone,
                    args,
                )

            elif command == "CHART":
                await self._chart(
                    phone,
                    args,
                )

            elif command == "ALERT":
                await self._create_alert(
                    phone,
                    args,
                )

            elif command == "ALERTS":
                await self._list_alerts(
                    phone,
                )

            elif command == "DELETE":
                await self._delete(
                    phone,
                    args,
                )

            elif command == "SEARCH":
                await self._search(
                    phone,
                    args,
                )

            elif command == "BACKTEST":
                await self._backtest(
                    phone,
                    args,
                )
            else:
                # Friendly shortcut:
                # "BTCUSDT 1H" means chart
                # "BTCUSDT" means price.
                await self._shortcut(
                    phone,
                    cleaned,
                )

        except Exception as exc:
            LOGGER.exception(
                "Command failed: %s",
                cleaned,
            )

            await self._send_text(
                phone,
                f"❌ {exc}",
            )

    async def _price(
        self,
        phone: str,
        args: str,
    ) -> None:

        if not args:
            raise ValueError(
                "Usage: PRICE BTCUSDT"
            )

        raw = args.split()[0]

        ref = await self.market.resolve(
            raw
        )

        if ref.exchange != "mexc":
            raise ValueError("Only MEXC Futures markets are supported.")

        price = await self.market.price(ref.symbol)

        if price is None:
            raise ValueError(
                "Could not get the current price."
            )

        await self._send_text(
            phone,
            (
                f"💰 {ref.symbol}\n"
                f"Exchange: MEXC FUTURES\n"
                f"Price: ${fmt_price(price)}"
            ),
        )

    async def _analyze(
        self,
        phone: str,
        args: str,
    ) -> None:

        if not args:
            raise ValueError("Usage: ANALYZE BTCUSDT")

        raw_symbol = args.split()[0]

        try:
            data = await analyze_symbol(self.market, raw_symbol)
        except TypeError as exc:
            # Some legacy analysis paths can receive an incomplete numeric value.
            # Do not expose a Python traceback/type error to the WhatsApp user.
            if "NoneType" in str(exc) or "abs()" in str(exc):
                LOGGER.exception("Incomplete analysis data for %s", raw_symbol)
                await self._send_text(
                    phone,
                    f"⚠️ Analysis data for {normalize_symbol_token(raw_symbol)} is incomplete.\nTry again after the next candle update.",
                )
                return
            raise

        def fmt_optional(value: object) -> str:
            if value is None:
                return "N/A"
            try:
                return fmt_price(float(value))
            except (TypeError, ValueError):
                return "N/A"

        def fmt_number(value: object, digits: int = 1) -> str:
            if value is None:
                return "N/A"
            try:
                return f"{float(value):.{digits}f}"
            except (TypeError, ValueError):
                return "N/A"

        body = (
            f"ðŸ§  {data.get('symbol', normalize_symbol_token(raw_symbol))} ANALYSIS\n\n"
            f"4H Trend: {data.get('trend_4h', 'N/A')}\n"
            f"1H Structure: {data.get('structure_1h', 'N/A')}\n"
            f"4H BOS: {data.get('bos_4h', 'N/A')}\n"
            f"EMA 21/50: {data.get('ema_direction', 'N/A')}\n"
            f"RSI: {fmt_number(data.get('rsi'))}\n"
            f"Volume: {data.get('volume', 'N/A')}\n"
            f"Support: {fmt_optional(data.get('support'))}\n"
            f"Resistance: {fmt_optional(data.get('resistance'))}\n"
            f"Score: {data.get('score', 'N/A')}/100\n"
            f"Families: {data.get('confirmation_families_passed', data.get('confirmation_family_count', 0))}/{data.get('confirmation_families_available', 8)}\n\n"
            f"Potential Setup: {data.get('setup', 'NO TRADE')}\n"
        )

        entry = data.get("entry")
        if entry is not None:
            body += (
                f"Entry: {fmt_optional(entry)}\n"
                f"SL: {fmt_optional(data.get('stop_loss'))}\n"
                f"TP: {fmt_optional(data.get('tp'))} ({data.get('target_timeframe', 'HTF')})\n"
                f"RR: 1:{fmt_number(data.get('rr'), 2)}"
            )

        await self._send_text(phone, body)

    async def _chart(
        self,
        phone: str,
        args: str,
    ) -> None:

        parts = args.split()

        if len(parts) < 2:
            raise ValueError("Usage: CHART BTCUSDT 1H")

        raw_symbol = parts[0]
        raw_tf = parts[1]

        tf = TIMEFRAME_ALIASES.get(raw_tf.upper())
        if not tf:
            raise ValueError("Supported chart timeframes: 5M, 15M, 1H, 4H, 1D")

        ref = await self.market.resolve(raw_symbol)
        rows = await self.market.ohlcv(
            ref,
            tf,
            self.settings.chart_default_bars,
        )

        path = None
        try:
            path = await self.charts.render(ref, tf, rows)
            if path is None:
                raise RuntimeError("Chart renderer did not return an image file.")

            path = Path(path)
            if not path.is_file():
                raise RuntimeError("Chart image was not created.")

            await self._send_image(
                phone,
                path,
                caption=(
                    f"📊 {ref.exchange.upper()} {ref.symbol} • {tf.upper()}\n"
                    f"EMA 21 / 50 / 100 / 200"
                ),
            )
        finally:
            if path is not None:
                try:
                    Path(path).unlink(missing_ok=True)
                except (TypeError, ValueError, OSError):
                    LOGGER.warning("Could not remove temporary chart file: %r", path)

    async def _create_alert(
        self,
        phone: str,
        args: str,
    ) -> None:

        parts = args.split()

        if len(parts) != 3:
            raise ValueError(
                "Usage: ALERT BTCUSDT ABOVE 120000"
            )

        raw_symbol, condition, target_raw = parts

        condition = condition.lower()

        if condition not in {"above", "below"}:
            raise ValueError(
                "Condition must be ABOVE or BELOW"
            )

        try:
            target = float(
                target_raw.replace(",", "")
            )

        except ValueError as exc:
            raise ValueError(
                "Target must be a number."
            ) from exc

        if target <= 0:
            raise ValueError(
                "Target must be greater than zero."
            )

        ref = await self.market.resolve(
            raw_symbol
        )

        if ref.exchange != "mexc":
            raise ValueError("Only MEXC Futures markets are supported.")

        current = await self.market.price(ref.symbol)

        if current is None:
            raise ValueError(
                "Could not read the current MEXC price. "
                "Try again."
            )

        if (
            condition == "above"
            and current >= target
        ):
            raise ValueError(
                f"Current price ${fmt_price(current)} "
                f"is already at/above "
                f"${fmt_price(target)}."
            )

        if (
            condition == "below"
            and current <= target
        ):
            raise ValueError(
                f"Current price ${fmt_price(current)} "
                f"is already at/below "
                f"${fmt_price(target)}."
            )

        alert = self.db.create_alert(
            phone,
            "mexc",
            ref.symbol,
            condition,
            target,
        )

        await self._send_text(
            phone,
            (
                f"✅ Alert #{alert.id} created\n\n"
                f"{ref.symbol}\n"
                f"Condition: "
                f"{condition.upper()} "
                f"${fmt_price(target)}\n"
                f"Current: "
                f"${fmt_price(current)}\n\n"
                f"You will receive one alert "
                f"when price crosses the target."
            ),
        )

    async def _list_alerts(
        self,
        phone: str,
    ) -> None:

        alerts = self.db.list_alerts(
            phone
        )

        if not alerts:
            await self._send_text(
                phone,
                "🔔 No active alerts.",
            )
            return

        lines = [
            "🔔 ACTIVE ALERTS",
            "",
        ]

        for alert in alerts:
            lines.append(
                f"#{alert.id} "
                f"{alert.symbol} "
                f"{alert.condition.upper()} "
                f"${fmt_price(alert.target)}"
            )

        lines.append("")
        lines.append(
            "DELETE <id> to remove an alert."
        )

        await self._send_text(
            phone,
            "\n".join(lines),
        )

    async def _delete(
        self,
        phone: str,
        args: str,
    ) -> None:

        if not args:
            raise ValueError(
                "Usage: DELETE 12 or DELETE ALL"
            )

        token = args.strip().upper()

        if token == "ALL":

            count = self.db.deactivate_all(
                phone
            )

            await self._send_text(
                phone,
                f"ðŸ—‘ï¸ Deleted {count} active alert(s).",
            )

            return

        try:
            alert_id = int(token)

        except ValueError as exc:
            raise ValueError(
                "Alert id must be a number or ALL."
            ) from exc

        if self.db.deactivate(
            alert_id,
            phone,
        ):

            await self._send_text(
                phone,
                f"ðŸ—‘ï¸ Alert #{alert_id} deleted.",
            )

        else:

            await self._send_text(
                phone,
                f"❌ Active alert #{alert_id} was not found.",
            )

    async def _search(
        self,
        phone: str,
        args: str,
    ) -> None:

        if not args:
            raise ValueError(
                "Usage: SEARCH PEPE"
            )

        results = await self.market.search(
            args,
            limit=20,
        )

        if not results:
            await self._send_text(
                phone,
                "No matching MEXC Futures markets found.",
            )
            return

        lines = [
            f"🔎 Matches for {args.upper()}:",
            "",
        ]

        for ref in results:
            lines.append(
                f"{ref.exchange.upper()}: {ref.symbol}"
            )

        lines.append("")
        lines.append(
            "Charts use MEXC Futures. Example: CHART MEXC:BTCUSDT 1H"
        )

        await self._send_text(
            phone,
            "\n".join(lines),
        )

    async def _backtest(
        self,
        phone: str,
        args: str,
    ) -> None:

        if self.backtest_runner is None:
            raise RuntimeError(
                "Backtest service is not configured."
            )

        period = args.strip().upper()
        periods = {
            "1D": 1,
            "7D": 7,
        }

        if period not in periods:
            raise ValueError(
                "Usage: BACKTEST 1D or BACKTEST 7D"
            )

        if self.backtest_runner.is_running:
            await self._send_text(
                phone,
                "⏳ A backtest is already running. Please wait for it to finish.",
            )
            return

        days = periods[period]

        await self._send_text(
            phone,
            (
                f"⏳ BACKTEST {period} STARTED\n\n"
                "Up to 200 eligible MEXC Futures coins will be tested.\n"
                "No real trades will be executed.\n\n"
                "I'll send the report here when finished."
            ),
        )

        try:
            summary = await self.backtest_runner.run(days)
            try:
                from .backtest.report import format_report
            except ImportError:
                from app.backtest.report import format_report

            await self._send_text(
                phone,
                format_report(summary),
            )
        except BacktestAlreadyRunning:
            await self._send_text(
                phone,
                "⏳ A backtest is already running. Please wait for it to finish.",
            )
        except Exception as exc:
            LOGGER.exception(
                "BACKTEST %s failed",
                period,
            )
            await self._send_text(
                phone,
                f"❌ BACKTEST {period} failed: {exc}",
            )
    async def _shortcut(
        self,
        phone: str,
        text: str,
    ) -> None:

        parts = text.split()

        if (
            len(parts) == 2
            and parts[1].upper()
            in TIMEFRAME_ALIASES
        ):

            await self._chart(
                phone,
                text,
            )

        elif len(parts) == 1:

            await self._price(
                phone,
                text,
            )

        else:

            await self._send_text(
                phone,
                HELP,
            )

    async def send_triggered_alert(
        self,
        alert: Alert,
        price: float,
    ) -> None:

        body = (
            f"ðŸš¨ PRICE ALERT\n\n"
            f"{alert.symbol}\n"
            f"MEXC FUTURES\n"
            f"Current: ${fmt_price(price)}\n"
            f"Target: ${fmt_price(alert.target)}\n"
            f"Condition: {alert.condition.upper()}\n\n"
            f"Alert #{alert.id} is now completed."
        )

        await self._send_text(
            alert.phone,
            body,
        )
