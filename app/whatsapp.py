"""Official WhatsApp Cloud API adapter with a hard crypto-policy gate."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from pathlib import Path
from typing import Any, Mapping

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class WhatsAppError(RuntimeError):
    pass


class WhatsAppPolicyBlocked(WhatsAppError):
    pass


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    if not signature_header or not app_secret or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def sender_key(value: str) -> str:
    raw = str(value).strip()
    if not raw or not re.fullmatch(r"[+0-9().\s-]+", raw):
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits if 7 <= len(digits) <= 16 else ""


def extract_incoming_messages(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes", []) if isinstance(entry.get("changes"), list) else []:
            if not isinstance(change, dict):
                continue
            value = change.get("value", {})
            if not isinstance(value, dict):
                continue
            incoming = value.get("messages", [])
            if not isinstance(incoming, list):
                continue
            for msg in incoming:
                if not isinstance(msg, dict):
                    continue
                msg_type = str(msg.get("type", ""))
                body = ""
                if msg_type == "text" and isinstance(msg.get("text"), dict):
                    body = str(msg["text"].get("body", ""))
                messages.append({
                    "message_id": str(msg.get("id", "")),
                    "from": sender_key(str(msg.get("from", ""))),
                    "timestamp": str(msg.get("timestamp", "")),
                    "type": msg_type,
                    "body": body[:500],
                    "phone_number_id": str(value.get("metadata", {}).get("phone_number_id", ""))
                    if isinstance(value.get("metadata"), dict) else "",
                })
    return messages


def extract_status_updates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract Cloud API delivery status updates; a send HTTP 200 is not delivery proof."""
    updates: list[dict[str, Any]] = []
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        return updates
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value", {})
            if not isinstance(value, dict):
                continue
            statuses = value.get("statuses", [])
            if not isinstance(statuses, list):
                continue
            for item in statuses:
                if not isinstance(item, dict):
                    continue
                errors = item.get("errors", [])
                error_text = None
                if isinstance(errors, list) and errors:
                    first = errors[0]
                    if isinstance(first, dict):
                        error_text = str(first.get("title") or first.get("message") or first.get("code") or "delivery failed")[:300]
                pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
                updates.append({
                    "provider_message_id": str(item.get("id", "")),
                    "status": str(item.get("status", "")).lower(),
                    "recipient_id": sender_key(str(item.get("recipient_id", ""))),
                    "timestamp": str(item.get("timestamp", "")),
                    "error": error_text,
                    "pricing": dict(pricing),
                })
    return updates


class WhatsAppCloud:
    """Cloud API sender. It refuses all app messaging unless the policy gate is explicitly open.

    Setting the gate is not proof of permission; the operator must retain actual written Meta
    clearance covering this exact crypto-related use before enabling it.
    """
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None,
                 graph_version: str = "v26.0") -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self.graph_version = graph_version

    def _require_gate(self) -> None:
        if not self.settings.whatsapp_policy_gate_open:
            raise WhatsAppPolicyBlocked(
                "WhatsApp crypto-related messaging is blocked until written Meta clearance is obtained and documented."
            )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _endpoint(self, suffix: str = "messages") -> str:
        if not self.settings.meta_phone_number_id:
            raise WhatsAppError("META_PHONE_NUMBER_ID is not configured")
        return f"https://graph.facebook.com/{self.graph_version}/{self.settings.meta_phone_number_id}/{suffix}"

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        self._require_gate()
        if not self.settings.meta_access_token:
            raise WhatsAppError("META_ACCESS_TOKEN is not configured")
        if not text or len(text) > 4000:
            raise WhatsAppError("Message is empty or exceeds configured size")
        allowed = {sender_key(x) for x in self.settings.whatsapp_allowed_senders}
        if allowed and sender_key(recipient) not in allowed:
            raise WhatsAppError("Recipient is not in the configured allowlist")
        client = await self._ensure_client()
        response = await client.post(
            self._endpoint(),
            headers={"Authorization": f"Bearer {self.settings.meta_access_token}"},
            json={"messaging_product": "whatsapp", "recipient_type": "individual",
                  "to": sender_key(recipient), "type": "text", "text": {"preview_url": False, "body": text}},
        )
        if response.is_error:
            # Do not log the request headers or token.
            raise WhatsAppError(f"Meta message send failed with HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise WhatsAppError("Meta message send returned invalid JSON") from exc
        return result if isinstance(result, dict) else {"response": "unstructured"}

    async def send_media(self, recipient: str, path: str | Path, *, media_type: str) -> dict[str, Any]:
        self._require_gate()
        if media_type not in {"image", "document"}:
            raise ValueError("media_type must be image or document")
        if not self.settings.meta_access_token:
            raise WhatsAppError("META_ACCESS_TOKEN is not configured")
        file_path = Path(path)
        if not file_path.is_file():
            raise WhatsAppError("Requested media file does not exist")
        allowed = {sender_key(x) for x in self.settings.whatsapp_allowed_senders}
        if allowed and sender_key(recipient) not in allowed:
            raise WhatsAppError("Recipient is not in the configured allowlist")
        client = await self._ensure_client()
        mime = "image/png" if media_type == "image" else "application/json"
        with file_path.open("rb") as handle:
            upload = await client.post(
                self._endpoint("media"), headers={"Authorization": f"Bearer {self.settings.meta_access_token}"},
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (file_path.name, handle, mime)},
            )
        if upload.is_error:
            raise WhatsAppError(f"Meta media upload failed with HTTP {upload.status_code}")
        try:
            media_id = upload.json()["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WhatsAppError("Meta media upload response lacked an id") from exc
        body: dict[str, Any] = {"messaging_product": "whatsapp", "to": sender_key(recipient), "type": media_type,
                                media_type: {"id": media_id}}
        if media_type == "document":
            body["document"]["filename"] = file_path.name
        response = await client.post(self._endpoint(),
            headers={"Authorization": f"Bearer {self.settings.meta_access_token}"}, json=body)
        if response.is_error:
            raise WhatsAppError(f"Meta media message failed with HTTP {response.status_code}")
        result = response.json()
        return result if isinstance(result, dict) else {"response": "unstructured"}
