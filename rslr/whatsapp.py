from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from rslr.charts import generate_chart
from rslr.config import settings
from rslr.data.candles import build_timeframes
from rslr.exchange.mexc import MEXCRestClient
from rslr.formatting import format_backtest, format_signal
from rslr.services import create_market_chart, scan_market
from rslr.storage import Database
from rslr.walk_forward import run_walk_forward

log = logging.getLogger(__name__)


class WhatsAppClient:
    def __init__(self) -> None:
        self.base = f"https://graph.facebook.com/{settings.whatsapp_graph_version}"
        self.http = httpx.Client(timeout=20.0)

    @property
    def configured(self) -> bool:
        return bool(settings.whatsapp_access_token and settings.whatsapp_phone_number_id)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("WhatsApp Cloud API credentials are not configured")
        response = self.http.post(
            f"{self.base}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            json=payload,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            try:
                detail = response.json().get("error", {}) or {}
            except ValueError:
                detail = {}
            log.error(
                "WhatsApp Graph API error: status=%s code=%s message=%s",
                response.status_code,
                detail.get("code"),
                detail.get("message"),
            )
            raise
        return response.json()

    def send_text(self, to: str, body: str) -> dict[str, Any]:
        return self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": body[:4000]},
            }
        )

    def send_template(self, to: str, body_text: str) -> dict[str, Any]:
        if not settings.whatsapp_signal_template_name:
            raise RuntimeError("No approved proactive signal template configured")
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": settings.whatsapp_signal_template_name,
                    "language": {"code": settings.whatsapp_template_language},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": body_text[:1024]}]}],
                },
            }
        )

    def send_image(self, to: str, image_path: str, caption: str) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("WhatsApp Cloud API credentials are not configured")
        url = f"{self.base}/{settings.whatsapp_phone_number_id}/media"
        with open(image_path, "rb") as image_file:
            uploaded = self.http.post(
                url,
                headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                data={"messaging_product": "whatsapp", "type": "image/png"},
                files={"file": ("rslr-chart.png", image_file, "image/png")},
            )
        uploaded.raise_for_status()
        media_id = uploaded.json()["id"]
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "image",
                "image": {"id": media_id, "caption": caption[:1024]},
            }
        )


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    if not settings.whatsapp_app_secret or not signature_header:
        return False
    expected = "sha256=" + hmac.new(settings.whatsapp_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def is_allowed_number(number: str) -> bool:
    normalized = "".join(ch for ch in number if ch.isdigit())
    return bool(settings.whatsapp_allowed_numbers) and normalized in {
        "".join(ch for ch in x if ch.isdigit()) for x in settings.whatsapp_allowed_numbers
    }


def command_help() -> str:
    return (
        "🤖 *RSLR COMMANDS*\n\n"
        "/menu or /help — command list\n"
        "/status — bot mode and risk\n"
        "/scan — scan MEXC futures\n"
        "/signals — latest signals\n"
        "/backtest 1D | 7D | 30D — historical test\n"
        "/chart BTC_USDT [1H|4H|12H|1D] — chart\n"
        "/performance — latest backtest summary\n"
        "/walkforward — 30D chronological validation\n"
        "/settings — active strategy settings\n"
        "/auto — live execution safety status"
    )


def handle_command(text: str, sender: str, database: Database | None = None) -> tuple[str | None, str | None]:
    """Return (text reply, optional image path). All commands fail closed on data errors."""
    database = database or Database()
    raw = text.strip()
    parts = raw.lstrip("/").split()
    if not parts:
        return command_help(), None
    command = parts[0].lower()

    if command in {"help", "menu"}:
        return command_help(), None
    if command == "status":
        return (
            "🟢 *RSLR STATUS*\n"
            "📡 Market data: MEXC Futures REST\n"
            f"🧭 Timeframes: 1D → 12H → 4H → 1H\n"
            f"⚖️ Risk: {settings.risk_per_trade * 100:.2f}% | Account basis: ${settings.account_equity_usd:.2f}\n"
            f"⚙️ Sweep: {'ON' if settings.enable_sweep else 'OFF'} | Donchian: {'ON' if settings.enable_donchian else 'OFF'}\n"
            f"🧪 Paper tracking: {'ON' if settings.paper_trading_enabled else 'OFF'}\n"
            "🔒 Live trading: HARD LOCKED",
            None,
        )
    if command == "auto":
        return "🔒 LIVE EXECUTION IS HARD-LOCKED IN SOURCE CODE. No WhatsApp command can enable it.", None
    if command == "settings":
        return (
            "⚙️ *RSLR SETTINGS*\n"
            f"Risk: {settings.risk_per_trade * 100:.2f}% equity\n"
            "Target: single 1R gross TP\n"
            f"Leverage cap: {settings.max_leverage}x\n"
            f"Open positions cap: {settings.max_open_positions}\n"
            f"Max alerts: {settings.max_signals_per_scan}/scan, {settings.max_signals_per_24h}/24h\n"
            f"Cooldown: {settings.same_symbol_cooldown_hours}h\n"
            f"Paper tracking: {'ON' if settings.paper_trading_enabled else 'OFF'}\n"
            "Live execution: DISABLED",
            None,
        )
    if command == "signals":
        items = database.latest_signals(5)
        if not items:
            return "No saved signals yet. Use /scan to check the market.", None
        return "📡 *LATEST SIGNALS*\n\n" + "\n\n".join(
            f"{x['symbol']} {x['side']} | {x['module']} | Entry {x['reference_entry']:.6g} | SL {x['stop_loss']:.6g} | TP {x['take_profit']:.6g}"
            for x in items
        ), None
    if command == "scan":
        signals = scan_market(database=database)
        if not signals:
            return "🔎 Scan complete. No new signals passed all validation checks.", None
        return "🔎 *SCAN COMPLETE*\n\n" + "\n\n".join(format_signal(x) for x in signals), None
    if command == "backtest":
        if len(parts) != 2 or parts[1].lower() not in {"1d", "7d", "30d"}:
            return "Use: /backtest 1D, /backtest 7D or /backtest 30D", None
        # Never run the backtest inline: it fetches days of history and would
        # block the webhook past Meta's response window. Queue a persistent
        # job and reply immediately; the worker runs it and reports back.
        period = parts[1].upper()
        job_id = database.enqueue_backtest_job(period, sender)
        log.info("Backtest job #%s (%s) queued for sender ****%s", job_id, period, sender[-4:])
        return (
            f"🧪 Backtest {period} queued — job #{job_id}.\n"
            "Running in the background; I'll send the results here when it's done.",
            None,
        )
    if command == "performance":
        result = database.latest_backtest()
        if not result:
            return "No backtest results saved. Run /backtest 1D, 7D or 30D.", None
        return format_backtest(result), None
    if command == "walkforward":
        result = run_walk_forward("30D")
        train = result["in_sample"]
        test = result["out_of_sample"]
        return (
            "🧪 *30D WALK-FORWARD*\n"
            f"Split: {result['split_fraction'] * 100:.0f}% IS / {100 - result['split_fraction'] * 100:.0f}% OOS\n"
            f"Boundary: {result['boundary']}\n\n"
            f"📘 IS: {train['total_signals']} trades | Net {train['net_pnl_usd']:+.2f} USD | DD {train['max_drawdown_pct']:.2f}%\n"
            f"📙 OOS: {test['total_signals']} trades | Net {test['net_pnl_usd']:+.2f} USD | DD {test['max_drawdown_pct']:.2f}%\n"
            f"⚠️ {result['warning']}"
        ), None
    if command == "chart":
        if len(parts) not in {2, 3}:
            return "Use: /chart BTC_USDT [1H|4H|12H|1D]", None
        symbol = parts[1].upper()
        timeframe = parts[2].upper() if len(parts) == 3 else "1H"
        if not symbol.endswith("_USDT"):
            return "Use a MEXC USDT-M symbol, e.g. /chart BTC_USDT 4H", None
        path = create_market_chart(symbol, timeframe, database=database)
        return f"📊 {symbol} {timeframe} — latest completed MEXC candle", str(path)
    return "Unknown command. Send /help for the command list.", None


def process_webhook_payload(payload: dict[str, Any], database: Database | None = None) -> None:
    database = database or Database()
    whatsapp = WhatsAppClient()
    entries = payload.get("entry", [])
    log.info("Webhook payload received: %s entries", len(entries))
    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            statuses = value.get("statuses", [])
            messages = value.get("messages", [])
            log.debug("Webhook change: %s statuses, %s messages", len(statuses), len(messages))
            for status_event in statuses:
                external_id = str(status_event.get("id") or "")
                status = str(status_event.get("status") or "").upper()
                errors = status_event.get("errors") or []
                error_text = json.dumps(errors, default=str) if errors else ""
                if external_id and status:
                    try:
                        matched = database.update_notification_by_external_id(external_id, status, error_text)
                        log.debug("Notification status %s -> %s (matched=%s)", external_id, status, matched)
                    except Exception:
                        log.exception("Notification status update failed for %s", external_id)
            for message in messages:
                sender = str(message.get("from", ""))
                message_type = str(message.get("type", ""))
                log.info("WhatsApp message received: sender ****%s type=%s", sender[-4:], message_type)
                if not is_allowed_number(sender):
                    log.warning("Ignoring WhatsApp sender not in allowlist: %s", sender[-4:])
                    continue
                text = message.get("text", {}).get("body", "")
                if message_type != "text" or not text:
                    try:
                        whatsapp.send_text(sender, "Please send a text command. Use /help.")
                    except Exception:
                        log.exception("Failed to send non-text notice to sender ****%s", sender[-4:])
                    continue
                command_name = text.strip().split()[0][:20] if text.strip() else ""
                log.info("Executing command %s for sender ****%s", command_name, sender[-4:])
                try:
                    reply, image_path = handle_command(text, sender, database)
                    if reply:
                        whatsapp.send_text(sender, reply)
                    if image_path:
                        whatsapp.send_image(sender, image_path, reply or "RSLR chart")
                    log.info("Command %s completed for sender ****%s", command_name, sender[-4:])
                except Exception as exc:
                    log.exception("WhatsApp command failed for sender ****%s", sender[-4:])
                    try:
                        whatsapp.send_text(sender, f"⚠️ Command failed: {str(exc)[:250]}")
                    except Exception:
                        log.exception("Failed to send command-failure notice to sender ****%s", sender[-4:])
