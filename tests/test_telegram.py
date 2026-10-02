from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import Settings
from app.telegram import TelegramClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400
        self.text = str(payload)

    def json(self):
        return self._payload


def test_telegram_config_and_recipient_parsing():
    settings = Settings(
        telegram_bot_token="TOKEN",
        telegram_allowed_users="123,-456",
        allowed_users="923001234567",
        auto_signal_telegram_recipients="789",
    )
    assert settings.telegram_allowed_user_set == {"tg:123", "tg:-456"}
    assert settings.auto_signal_recipient_set == {"923001234567", "tg:789"}


def test_telegram_client_send_text(monkeypatch):
    client = TelegramClient("TOKEN")
    calls = []

    async def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"ok": True, "result": {"message_id": 1}})

    client.http.post = fake_post
    result = asyncio.run(client.send_text("tg:123", "hello"))
    asyncio.run(client.close())

    assert result["ok"] is True
    assert calls[0][0].endswith("/sendMessage")
    assert calls[0][1]["json"]["chat_id"] == "123"
    assert calls[0][1]["json"]["text"] == "hello"


def test_telegram_client_chunks_long_text(monkeypatch):
    client = TelegramClient("TOKEN")
    calls = []

    async def fake_post(url, **kwargs):
        calls.append(kwargs["json"]["text"])
        return FakeResponse({"ok": True, "result": {"message_id": len(calls)}})

    client.http.post = fake_post
    asyncio.run(client.send_text("tg:123", "x" * 5000))
    asyncio.run(client.close())

    assert [len(v) for v in calls] == [4096, 904]


def test_bot_routes_telegram_command_without_affecting_whatsapp():
    from app.bot import Bot
    from app.charts import ChartRenderer
    from app.database import Database
    from app.market import MarketRef

    class FakeWhatsApp:
        def __init__(self):
            self.texts = []

        async def send_text(self, to, body):
            self.texts.append((to, body))

    class FakeTelegram:
        def __init__(self):
            self.texts = []

        async def send_text(self, to, body):
            self.texts.append((to, body))

    class FakeMarket:
        async def resolve(self, raw):
            return MarketRef("mexc", raw.upper())

        async def price(self, symbol):
            return 123.45

    wa = FakeWhatsApp()
    tg = FakeTelegram()
    bot = Bot(
        settings=Settings(
            allowed_users="923001234567",
            telegram_allowed_users="777",
        ),
        db=Database(":memory:"),
        market=FakeMarket(),
        whatsapp=wa,
        telegram=tg,
        charts=ChartRenderer(),
    )

    asyncio.run(bot.handle("tg:777", "/price BTCUSDT"))

    assert tg.texts == [
        ("tg:777", "💰 BTCUSDT\nExchange: MEXC FUTURES\nPrice: $123.45")
    ]
    assert wa.texts == []
