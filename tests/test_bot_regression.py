from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.bot import Bot
from app.charts import ChartRenderer
from app.config import Settings
from app.database import Database
from app.market import MarketRef


class FakeWhatsApp:
    def __init__(self):
        self.texts = []
        self.images = []

    async def send_text(self, to, body):
        self.texts.append((to, body))

    async def upload_image(self, path):
        return "media-id"

    async def send_image(self, to, media_id, caption=None):
        self.images.append((to, media_id, caption))


class FakeMarket:
    async def resolve(self, raw):
        return MarketRef("mexc", raw.upper().replace("/", "_"))

    async def price(self, symbol):
        return 1234.56



def make_bot(tmp_path):
    return Bot(
        settings=Settings(allowed_users="923001234567"),
        db=Database(str(tmp_path / "bot.sqlite3")),
        market=FakeMarket(),
        whatsapp=FakeWhatsApp(),
        charts=ChartRenderer(),
    )


def test_price_command_still_works(tmp_path):
    bot = make_bot(tmp_path)
    asyncio.run(bot.handle("923001234567", "PRICE BTCUSDT"))
    assert bot.whatsapp.texts == [
        ("923001234567", "💰 BTCUSDT\nExchange: MEXC FUTURES\nPrice: $1,234.56")
    ]


def test_help_and_private_gate(tmp_path):
    bot = make_bot(tmp_path)
    asyncio.run(bot.handle("923001234567", "HELP"))
    assert "PRICE BTCUSDT" in bot.whatsapp.texts[0][1]

    bot.whatsapp.texts.clear()
    asyncio.run(bot.handle("923999999999", "PRICE BTCUSDT"))
    assert bot.whatsapp.texts == [("923999999999", "⛔ This bot is private.")]
