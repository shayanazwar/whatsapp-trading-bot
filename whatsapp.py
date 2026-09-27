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
                "body": str(body),
            },
        }

        return await self._post(
            f"/{self.phone_number_id}/messages",
            payload=payload,
        )

    async def upload_image(self, path: Path) -> str:
        if not isinstance(path, Path):
            path = Path(path)

        if not path.exists() or not path.is_file():
            raise WhatsAppError(
                f"Chart image file does not exist: {path}"
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

        try:
            data = response.json()
        except ValueError as exc:
            raise WhatsAppError(
                "Media upload returned invalid JSON."
            ) from exc

        media_id = data.get("id") if isinstance(data, dict) else None

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
            "id": str(media_id),
        }

        if caption:
            image_payload["caption"] = str(caption)

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": image_payload,
        }

        return await self._post(
            f"/{self.phone_number_id}/messages",
            payload=payload,
        )

    async def _post(
        self,
        path: str,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Send a WhatsApp Graph API JSON request.

        Unicode is deliberately JSON-escaped before transmission.
        This prevents UTF-8 characters such as emojis and bullets from
        being displayed as mojibake such as 'Ã°Å¸' or 'Ã¢â‚¬Â¢' by clients.
        """

        body = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")

        response = await self.http.post(
            f"{self.base}{path}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            content=body,
        )

        if response.is_error:
            raise WhatsAppError(
                f"WhatsApp API failed: "
                f"{response.status_code} {response.text}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise WhatsAppError(
                "WhatsApp API returned invalid JSON."
            ) from exc

        if not isinstance(data, dict):
            raise WhatsAppError(
                f"WhatsApp API returned unexpected response: {data}"
            )

        return data
