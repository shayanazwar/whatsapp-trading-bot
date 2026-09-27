from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)


class WhatsAppError(RuntimeError):
    pass


class WhatsAppClient:
    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        graph_version: str,
        app_secret: str,
    ) -> None:
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.graph_version = graph_version
        self.app_secret = app_secret
        self.base = f"https://graph.facebook.com/{graph_version}"

        self.http = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self.http.aclose()

    def verify_signature(
        self,
        raw_body: bytes,
        signature_header: str | None,
    ) -> bool:
        if not self.app_secret:
            return False

        if not signature_header or not signature_header.startswith("sha256="):
            return False

        expected = hmac.new(
            self.app_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        provided = signature_header.split("=", 1)[1]

        return hmac.compare_digest(expected, provided)

    async def send_text(
        self,
        to: str,
        body: str,
    ) -> dict[str, Any]:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": body,
            },
        }

        return await self._post(
            f"/{self.phone_number_id}/messages",
            payload,
        )

    async def upload_image(self, path: Path) -> str:
        if not path.exists():
            raise WhatsAppError(
                f"Image file does not exist: {path}"
            )

        if not path.is_file():
            raise WhatsAppError(
                f"Image path is not a file: {path}"
            )

        with path.open("rb") as file_obj:
            response = await self.http.post(
                f"{self.base}/{self.phone_number_id}/media",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                },
                data={
                    "messaging_product": "whatsapp",
                    "type": "image/png",
                },
                files={
                    "file": (
                        path.name,
                        file_obj,
                        "image/png",
                    )
                },
            )

        if response.is_error:
            raise WhatsAppError(
                f"Media upload failed: "
                f"{response.status_code} {response.text}"
            )

        data = response.json()

        media_id = data.get("id")

        if not media_id:
            raise WhatsAppError(
                f"Media upload returned no id: {data}"
            )

        return str(media_id)

    async def send_image(
        self,
        to: str,
        media_id: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        image_payload: dict[str, Any] = {
            "id": media_id,
        }

        if caption:
            image_payload["caption"] = caption

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": image_payload,
        }

        return await self._post(
            f"/{self.phone_number_id}/messages",
            payload,
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Send WhatsApp Graph API JSON using explicit UTF-8 bytes.

        ensure_ascii=False is important so Unicode characters such as:
        📈 💰 📊 🔔 🔎 ⏱ •
        remain proper Unicode characters instead of being converted
        through an incorrect character encoding.
        """

        json_text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        json_bytes = json_text.encode("utf-8")

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        response = await self.http.post(
            f"{self.base}{path}",
            headers=headers,
            content=json_bytes,
        )

        if response.is_error:
            raise WhatsAppError(
                f"WhatsApp API failed: "
                f"{response.status_code} {response.text}"
            )

        return response.json()
