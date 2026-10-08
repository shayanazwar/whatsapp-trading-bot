from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    """Raised when the Telegram Bot API returns an error."""


class TelegramClient:
    """Small async Telegram Bot API client used by the dual-channel bot."""

    def __init__(self, token: str) -> None:
        self.token = token.strip()
        self.base = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.http = httpx.AsyncClient(timeout=30)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def close(self) -> None:
        await self.http.aclose()

    async def _post(self, method: str, **kwargs: Any) -> dict[str, Any]:
        if not self.configured:
            raise TelegramError("Telegram Bot API is not configured.")

        response = await self.http.post(f"{self.base}/{method}", **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Telegram API returned invalid JSON: {response.status_code} {response.text}"
            ) from exc

        if response.is_error or not data.get("ok", False):
            description = data.get("description") or response.text
            raise TelegramError(
                f"Telegram API failed: {response.status_code} {description}"
            )

        return data

    @staticmethod
    def _chat_id(target: str) -> str:
        if target.startswith("tg:"):
            return target[3:]
        if target.startswith("telegram:"):
            return target[9:]
        return target

    async def send_text(self, target: str, body: str) -> dict[str, Any]:
        """Send one or more Telegram messages, respecting Telegram's 4096-char limit."""
        chat_id = self._chat_id(target)
        text = body or ""
        chunks = [text[i : i + 4096] for i in range(0, len(text), 4096)] or [""]

        result: dict[str, Any] = {}
        for chunk in chunks:
            result = await self._post(
                "sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
            )
        return result

    async def send_image(
        self,
        target: str,
        path: str | Path,
        caption: str | None = None,
    ) -> dict[str, Any]:
        image_path = Path(path)
        if not image_path.exists() or not image_path.is_file():
            raise TelegramError(f"Image file does not exist: {image_path}")

        chat_id = self._chat_id(target)
        data: dict[str, str] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]

        with image_path.open("rb") as file_obj:
            return await self._post(
                "sendPhoto",
                data=data,
                files={"photo": (image_path.name, file_obj, "image/png")},
            )

    async def set_webhook(
        self,
        url: str,
        *,
        secret_token: str = "",
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": url,
            "drop_pending_updates": drop_pending_updates,
        }
        if secret_token:
            payload["secret_token"] = secret_token
        return await self._post("setWebhook", json=payload)

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, Any]:
        return await self._post(
            "deleteWebhook",
            json={"drop_pending_updates": drop_pending_updates},
        )

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._post("getWebhookInfo", json={})

    async def set_my_commands(self) -> dict[str, Any]:
        commands = [
            {"command": "start", "description": "Open the trading bot menu"},
            {"command": "help", "description": "Show available commands"},
            {"command": "price", "description": "Get a MEXC Futures price"},
            {"command": "analyze", "description": "Analyze a MEXC Futures symbol"},
            {"command": "chart", "description": "Generate a market chart"},
            {"command": "alert", "description": "Create a price alert"},
            {"command": "alerts", "description": "List active alerts"},
            {"command": "delete", "description": "Delete an alert"},
            {"command": "search", "description": "Search MEXC Futures symbols"},
            {"command": "backtest", "description": "Run a paper backtest"},
        ]
        return await self._post("setMyCommands", json={"commands": commands})
