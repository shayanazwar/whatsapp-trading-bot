
"""Official WhatsApp Cloud API adapter with a hard crypto-policy gate."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from pathlib import Path
from typing import Any, Mapping

import httpx

from app.commands import WHATSAPP_SUPPORT_COMMANDS, WHATSAPP_SUPPORT_HELP_TEXT
from app.config import Settings

logger = logging.getLogger(__name__)


class WhatsAppError(RuntimeError):
    """Raised when a WhatsApp Cloud API operation fails."""


class WhatsAppPolicyBlocked(WhatsAppError):
    """Raised when crypto-related messaging is not approved."""


# One canonical payload is shared by the sender and the durable outbox so the
# recorded message always matches the message actually submitted to Meta.
WHATSAPP_POLICY_BLOCKED_REPLY = (
    "Research commands are not enabled on this WhatsApp channel. "
    "Open the MEXC Research Dashboard: https://shayan-millionaire-bot.onrender.com/dashboard"
)


def verify_webhook_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    """Verify Meta's X-Hub-Signature-256 header."""
    if (
        not signature_header
        or not app_secret
        or not signature_header.startswith("sha256=")
    ):
        return False

    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)


def sender_key(value: str) -> str:
    """Normalize a phone number to digits only."""
    raw = str(value).strip()

    if not raw or not re.fullmatch(r"[+0-9().\s-]+", raw):
        return ""

    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits if 7 <= len(digits) <= 16 else ""


def extract_incoming_messages(
    payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Extract incoming messages from a Meta webhook payload."""
    messages: list[dict[str, str]] = []

    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        return messages

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

            incoming = value.get("messages", [])
            if not isinstance(incoming, list):
                continue

            metadata = value.get("metadata", {})
            phone_number_id = (
                str(metadata.get("phone_number_id", ""))
                if isinstance(metadata, dict)
                else ""
            )

            for msg in incoming:
                if not isinstance(msg, dict):
                    continue

                msg_type = str(msg.get("type", ""))
                body = ""

                if msg_type == "text":
                    text_data = msg.get("text", {})
                    if isinstance(text_data, dict):
                        body = str(text_data.get("body", ""))

                messages.append(
                    {
                        "message_id": str(msg.get("id", "")),
                        "from": sender_key(str(msg.get("from", ""))),
                        "timestamp": str(msg.get("timestamp", "")),
                        "type": msg_type,
                        "body": body[:500],
                        "phone_number_id": phone_number_id,
                    }
                )

    return messages


def extract_status_updates(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Extract message delivery status updates from Meta webhooks."""
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
                        error_text = str(
                            first.get("title")
                            or first.get("message")
                            or first.get("code")
                            or "delivery failed"
                        )[:300]

                pricing = item.get("pricing")
                if not isinstance(pricing, dict):
                    pricing = {}

                updates.append(
                    {
                        "provider_message_id": str(item.get("id", "")),
                        "status": str(item.get("status", "")).lower(),
                        "recipient_id": sender_key(
                            str(item.get("recipient_id", ""))
                        ),
                        "timestamp": str(item.get("timestamp", "")),
                        "error": error_text,
                        "pricing": dict(pricing),
                    }
                )

    return updates


class WhatsAppCloud:
    """WhatsApp Cloud API adapter.

    Crypto/trading messages remain blocked unless the policy gate is
    explicitly enabled after the required written clearance is obtained.
    A generic service-status reply does not unlock trading functionality.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        graph_version: str = "v26.0",
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self.graph_version = graph_version

    def _require_gate(self) -> None:
        if not self.settings.whatsapp_policy_gate_open:
            raise WhatsAppPolicyBlocked(
                "Crypto-related WhatsApp messaging is blocked until "
                "written Meta clearance is obtained and documented."
            )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0)
            )
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client only when this adapter owns it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _endpoint(self, suffix: str = "messages") -> str:
        phone_number_id = self.settings.meta_phone_number_id

        if not phone_number_id:
            raise WhatsAppError(
                "META_PHONE_NUMBER_ID is not configured"
            )

        return (
            f"https://graph.facebook.com/{self.graph_version}/"
            f"{phone_number_id}/{suffix}"
        )

    def _validate_recipient(self, recipient: str) -> str:
        """Validate and normalize an allowlisted recipient."""
        recipient_key = sender_key(recipient)

        allowed = {
            sender_key(value)
            for value in self.settings.whatsapp_allowed_senders
        }
        allowed.discard("")

        if not recipient_key or recipient_key not in allowed:
            raise WhatsAppError(
                "Recipient is not in the configured allowlist"
            )

        return recipient_key

    async def send_service_status(
        self,
        recipient: str,
    ) -> dict[str, Any]:
        """Send a fixed generic service reply without trading information.

        This path deliberately does not open the crypto policy gate.
        The recipient must be explicitly allowlisted.
        """
        recipient_key = self._validate_recipient(recipient)

        if not self.settings.meta_access_token:
            raise WhatsAppError(
                "META_ACCESS_TOKEN is not configured"
            )

        text = WHATSAPP_POLICY_BLOCKED_REPLY

        client = await self._ensure_client()
        response = await client.post(
            self._endpoint(),
            headers={
                "Authorization": (
                    f"Bearer {self.settings.meta_access_token}"
                )
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_key,
                "type": "text",
                "text": {
                    "preview_url": False,
                    "body": text,
                },
            },
        )

        if response.is_error:
            logger.warning(
                "Meta service-status send failed | HTTP %s",
                response.status_code,
            )
            raise WhatsAppError(
                "Meta service-status message failed with HTTP "
                f"{response.status_code}"
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise WhatsAppError(
                "Meta returned invalid JSON for service-status message"
            ) from exc

        return result if isinstance(result, dict) else {}

    async def send_text(
        self,
        recipient: str,
        text: str,
    ) -> dict[str, Any]:
        """Send research text only when the explicit policy gate is open."""
        self._require_gate()
        return await self._send_text_payload(recipient, text)

    async def send_support_text(
        self,
        recipient: str,
        text: str,
        *,
        command: str,
    ) -> dict[str, Any]:
        """Send only the fixed HELP menu or a deterministic STATUS reply with the gate closed."""
        command = str(command).upper().strip()
        if command not in WHATSAPP_SUPPORT_COMMANDS:
            raise WhatsAppPolicyBlocked("Only HELP and STATUS can be sent through the support path.")
        if command == "HELP" and text != WHATSAPP_SUPPORT_HELP_TEXT:
            raise WhatsAppPolicyBlocked("HELP content must match the approved deterministic menu.")
        if command == "STATUS" and not text.startswith("SYSTEM STATUS\n"):
            raise WhatsAppPolicyBlocked("STATUS content must be a deterministic service-status reply.")
        return await self._send_text_payload(recipient, text)

    async def _send_text_payload(self, recipient: str, text: str) -> dict[str, Any]:
        if not self.settings.meta_access_token:
            raise WhatsAppError("META_ACCESS_TOKEN is not configured")
        if not text or len(text) > 4000:
            raise WhatsAppError("Message is empty or exceeds configured size")
        recipient_key = self._validate_recipient(recipient)
        client = await self._ensure_client()
        response = await client.post(
            self._endpoint(),
            headers={"Authorization": f"Bearer {self.settings.meta_access_token}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_key,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
        )
        if response.is_error:
            logger.warning("Meta text send failed | HTTP %s", response.status_code)
            raise WhatsAppError(f"Meta message send failed with HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise WhatsAppError("Meta message send returned invalid JSON") from exc
        return result if isinstance(result, dict) else {"response": "unstructured"}

    async def send_media(
        self,
        recipient: str,
        path: str | Path,
        *,
        media_type: str,
    ) -> dict[str, Any]:
        """Upload and send image/document media when the gate is open."""
        self._require_gate()

        if media_type not in {"image", "document"}:
            raise ValueError(
                "media_type must be image or document"
            )

        if not self.settings.meta_access_token:
            raise WhatsAppError(
                "META_ACCESS_TOKEN is not configured"
            )

        recipient_key = self._validate_recipient(recipient)
        file_path = Path(path)

        if not file_path.is_file():
            raise WhatsAppError(
                "Requested media file does not exist"
            )

        if media_type == "image":
            mime = "image/png"
        else:
            suffix = file_path.suffix.lower()
            allowed_doc_types = {
                ".pdf": "application/pdf",
                ".txt": "text/plain",
                ".csv": "text/csv",
            }
            if suffix not in allowed_doc_types:
                raise WhatsAppError("WhatsApp report attachments must be PDF, TXT or CSV documents")
            mime = allowed_doc_types[suffix]

        client = await self._ensure_client()
        headers = {
            "Authorization": (
                f"Bearer {self.settings.meta_access_token}"
            )
        }

        with file_path.open("rb") as handle:
            upload = await client.post(
                self._endpoint("media"),
                headers=headers,
                data={
                    "messaging_product": "whatsapp",
                    "type": mime,
                },
                files={
                    "file": (file_path.name, handle, mime)
                },
            )

        if upload.is_error:
            logger.warning(
                "Meta media upload failed | HTTP %s",
                upload.status_code,
            )
            raise WhatsAppError(
                "Meta media upload failed with HTTP "
                f"{upload.status_code}"
            )

        try:
            upload_result = upload.json()
            media_id = upload_result["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WhatsAppError(
                "Meta media upload response lacked an id"
            ) from exc

        media_content: dict[str, Any] = {"id": media_id}

        if media_type == "document":
            media_content["filename"] = file_path.name

        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_key,
            "type": media_type,
            media_type: media_content,
        }

        response = await client.post(
            self._endpoint(),
            headers=headers,
            json=body,
        )

        if response.is_error:
            logger.warning(
                "Meta media send failed | HTTP %s",
                response.status_code,
            )
            raise WhatsAppError(
                "Meta media message failed with HTTP "
                f"{response.status_code}"
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise WhatsAppError(
                "Meta media send returned invalid JSON"
            ) from exc

        return (
            result
            if isinstance(result, dict)
            else {"response": "unstructured"}
        )
