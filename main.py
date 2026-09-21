from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .alerts import AlertEngine
from .bot import Bot
from .charts import ChartRenderer
from .config import get_settings
from .database import Database
from .market import MarketData
from .whatsapp import WhatsAppClient

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
LOGGER = logging.getLogger(__name__)

db = Database()
market = MarketData(settings)
whatsapp = WhatsAppClient(
    settings.meta_access_token,
    settings.meta_phone_number_id,
    settings.meta_graph_version,
    settings.meta_app_secret,
)
charts = ChartRenderer(settings.chart_default_bars)
bot = Bot(settings, db, market, whatsapp, charts)
alert_engine = AlertEngine(db, market, settings, bot.send_triggered_alert)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await market.start()
    await alert_engine.start()
    try:
        yield
    finally:
        await alert_engine.stop()
        await market.close()
        await whatsapp.close()


app = FastAPI(title="WhatsApp Trading Bot", version="1.0.0", lifespan=lifespan)


@app.get("/", response_class=PlainTextResponse)
async def root() -> str:
    return "WhatsApp Trading Bot is running."


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "binance_cached_symbols": len(market.binance_stream.prices)}


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(request: Request) -> PlainTextResponse:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == settings.meta_verify_token and challenge:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook", response_class=PlainTextResponse)
async def receive_webhook(request: Request) -> PlainTextResponse:
    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not whatsapp.verify_signature(raw, signature):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    # Always return 200 promptly; processing itself is scheduled separately.
    asyncio.create_task(process_webhook(payload))
    return PlainTextResponse("EVENT_RECEIVED", status_code=200)


async def process_webhook(payload: dict[str, Any]) -> None:
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []) or []:
                    message_id = message.get("id")
                    if not message_id or not db.mark_message_seen(message_id):
                        continue
                    message_type = message.get("type")
                    if message_type != "text":
                        continue
                    sender = message.get("from")
                    text_body = message.get("text", {}).get("body", "")
                    if sender and text_body:
                        await bot.handle(str(sender), str(text_body))
    except Exception:
        LOGGER.exception("Webhook processing failed")
