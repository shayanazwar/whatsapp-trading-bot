from __future__ import annotations

from pathlib import Path

from .telegram import TelegramClient, TelegramError
from .whatsapp import WhatsAppClient, WhatsAppError


def normalize_telegram_target(value: str | int) -> str:
    return f"tg:{str(value).strip()}"


def is_telegram_target(target: str) -> bool:
    return target.startswith("tg:") or target.startswith("telegram:")


class ChannelRouter:
    """Route outbound messages to WhatsApp or Telegram by target prefix."""

    def __init__(
        self,
        *,
        whatsapp: WhatsAppClient,
        telegram: TelegramClient,
    ) -> None:
        self.whatsapp = whatsapp
        self.telegram = telegram

    async def send_text(self, target: str, body: str):
        if is_telegram_target(target):
            return await self.telegram.send_text(target, body)
        return await self.whatsapp.send_text(target, body)

    async def send_image(self, target: str, path: str | Path, caption: str | None = None):
        if is_telegram_target(target):
            return await self.telegram.send_image(target, path, caption)
        media_id = await self.whatsapp.upload_image(Path(path))
        return await self.whatsapp.send_image(target, media_id, caption)

    def is_configured(self, target: str) -> bool:
        if is_telegram_target(target):
            return self.telegram.configured
        return bool(self.whatsapp.access_token and self.whatsapp.phone_number_id)
