from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class WhatsAppNotConfigured(RuntimeError):
    pass


class WhatsAppClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(20))
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.settings.wa_phone_number_id and self.settings.wa_access_token)

    def verify_signature(self, body: bytes, signature: str | None) -> bool:
        secret = self.settings.wa_app_secret
        if not secret or not signature or not signature.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def send_text(self, recipient: str, text: str) -> None:
        if not self.configured:
            raise WhatsAppNotConfigured("WhatsApp Cloud API credentials are not configured")
        url = f"https://graph.facebook.com/{self.settings.graph_api_version}/{self.settings.wa_phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self.settings.wa_access_token}"}
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient,
                   "type": "text", "text": {"preview_url": False, "body": text}}
        response = await self._client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            # Log only Meta's error code/message (never the message body or
            # credentials): without the code a 400 is undiagnosable from logs.
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
            raise RuntimeError(f"WhatsApp send failed with HTTP {response.status_code}")

    async def send_chunks(self, recipient: str, text: str, max_chars: int = 3500) -> None:
        paragraphs = text.split("\n")
        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            candidate = paragraph if not current else current + "\n" + paragraph
            if len(candidate) > max_chars and current:
                chunks.append(current)
                current = paragraph
            else:
                current = candidate
            while len(current) > max_chars:
                chunks.append(current[:max_chars])
                current = current[max_chars:]
        if current:
            chunks.append(current)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            prefix = f"[{index}/{total}]\n" if total > 1 else ""
            await self.send_text(recipient, prefix + chunk)


def extract_inbound_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for entry in payload.get("entry", []) if isinstance(payload.get("entry", []), list) else []:
        changes = entry.get("changes", []) if isinstance(entry, dict) else []
        if not isinstance(changes, list):
            continue
        for change in changes:
            value = change.get("value", {}) if isinstance(change, dict) else {}
            messages = value.get("messages", []) if isinstance(value, dict) else []
            if not isinstance(messages, list):
                continue
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                sender = str(msg.get("from", ""))
                msg_id = str(msg.get("id", ""))
                body = str(msg.get("text", {}).get("body", "")).strip() if isinstance(msg.get("text"), dict) else ""
                if sender and msg_id and body:
                    out.append({"sender": sender, "message_id": msg_id, "body": body})
    return out


def split_backtest_command(text: str) -> tuple[int, str] | None:
    parts = text.strip().lower().split()
    if len(parts) != 3 or parts[0] != "/backtest":
        return None
    period_map = {"1d": 1, "7d": 7, "30d": 30}
    direction_map = {"both": "BOTH", "long": "LONG", "short": "SHORT"}
    if parts[1] not in period_map or parts[2] not in direction_map:
        return None
    return period_map[parts[1]], direction_map[parts[2]]
