from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .alerts import AlertEngine
from .bot import Bot
from .charts import ChartRenderer
from .config import get_settings
from .database import Database
from .market import MarketData
from .whatsapp import WhatsAppClient


# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("whatsapp_bot")


# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

settings = get_settings()

if not settings.meta_phone_number_id:
    raise RuntimeError("META_PHONE_NUMBER_ID is not configured")

if not settings.meta_access_token:
    raise RuntimeError("META_ACCESS_TOKEN is not configured")

if not settings.meta_app_secret:
    raise RuntimeError("META_APP_SECRET is not configured")


# ---------------------------------------------------------
# APP SERVICES
# ---------------------------------------------------------

app = FastAPI(title="Pak Trading Academy WhatsApp Bot")

db = Database()
market = MarketData(settings)

whatsapp = WhatsAppClient(
    access_token=settings.meta_access_token,
    phone_number_id=settings.meta_phone_number_id,
    graph_version=settings.meta_graph_version,
    app_secret=settings.meta_app_secret,
)

charts = ChartRenderer(settings.chart_default_bars)

bot = Bot(
    settings=settings,
    db=db,
    market=market,
    whatsapp=whatsapp,
    charts=charts,
)

alert_engine = AlertEngine(
    db=db,
    market=market,
    settings=settings,
    on_trigger=bot.send_triggered_alert,
)

_background_tasks: set[asyncio.Task] = set()


def keep_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------
# HEALTH
# ---------------------------------------------------------

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Pak Trading Academy WhatsApp Bot",
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


# ---------------------------------------------------------
# META WEBHOOK VERIFICATION
# ---------------------------------------------------------

@app.get("/webhook")
async def verify_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(
        None,
        alias="hub.verify_token",
    ),
    hub_challenge: Optional[str] = Query(
        None,
        alias="hub.challenge",
    ),
):
    if (
        hub_mode == "subscribe"
        and hub_verify_token
        and settings.meta_verify_token
        and hmac.compare_digest(
            hub_verify_token,
            settings.meta_verify_token,
        )
    ):
        logger.info("WEBHOOK VERIFICATION SUCCESS")
        return PlainTextResponse(
            content=hub_challenge or "",
            status_code=200,
        )

    logger.warning("WEBHOOK VERIFICATION FAILED")

    return PlainTextResponse(
        content="Forbidden",
        status_code=403,
    )


# ---------------------------------------------------------
# PROCESS WHATSAPP EVENT
# ---------------------------------------------------------

async def process_webhook(payload: dict) -> None:
    logger.info("========== PROCESSING WHATSAPP EVENT ==========")

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):

            if change.get("field") != "messages":
                continue

            value = change.get("value", {})

            for message in value.get("messages", []):

                message_id = message.get("id")
                message_type = message.get("type")
                sender = message.get("from")

                logger.info(
                    "Incoming message: id=%s type=%s from=%s",
                    message_id,
                    message_type,
                    sender,
                )

                # Prevent duplicate processing when Meta retries events.
                if message_id and not db.mark_message_seen(message_id):
                    logger.info(
                        "Ignoring duplicate message: %s",
                        message_id,
                    )
                    continue

                if message_type != "text":
                    logger.info(
                        "Ignoring unsupported message type: %s",
                        message_type,
                    )
                    continue

                text = message.get("text", {}).get("body", "").strip()

                if not sender or not text:
                    continue

                logger.info("Incoming text: %s", text)

                try:
                    await bot.handle(sender, text)
                    logger.info(
                        "Bot command processed successfully."
                    )
                except Exception:
                    logger.exception(
                        "Bot command processing failed."
                    )


# ---------------------------------------------------------
# META WEBHOOK POST
# ---------------------------------------------------------

@app.post("/webhook")
async def receive_webhook(request: Request):

    body = await request.body()

    signature = request.headers.get("X-Hub-Signature-256")

    if not whatsapp.verify_signature(
        body,
        signature,
    ):
        logger.warning(
            "Webhook signature verification FAILED"
        )

        return JSONResponse(
            content={"status": "invalid_signature"},
            status_code=403,
        )

    logger.info(
        "Webhook signature verification PASSED"
    )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.exception(
            "Webhook body was not valid JSON."
        )

        return JSONResponse(
            content={"status": "invalid_json"},
            status_code=400,
        )

    task = asyncio.create_task(
        process_webhook(payload)
    )

    keep_task(task)

    # Acknowledge Meta immediately.
    return JSONResponse(
        content={"status": "ok"},
        status_code=200,
    )


# ---------------------------------------------------------
# STARTUP
# ---------------------------------------------------------

@app.on_event("startup")
async def startup_event():

    logger.info("==========================================")
    logger.info("Pak Trading Academy WhatsApp Bot starting")
    logger.info("==========================================")

    logger.info(
        "Phone Number ID: %s",
        settings.meta_phone_number_id,
    )

    logger.info(
        "Graph API version: %s",
        settings.meta_graph_version,
    )

    logger.info(
        "META_VERIFY_TOKEN configured: %s",
        bool(settings.meta_verify_token),
    )

    logger.info(
        "META_APP_SECRET configured: %s",
        bool(settings.meta_app_secret),
    )

    logger.info(
        "META_ACCESS_TOKEN configured: %s",
        bool(settings.meta_access_token),
    )

    await market.start()
    logger.info("Binance market-data service started.")

    await alert_engine.start()
    logger.info("Alert engine started.")

    logger.info("Bot startup complete.")


# ---------------------------------------------------------
# SHUTDOWN
# ---------------------------------------------------------

@app.on_event("shutdown")
async def shutdown_event():

    logger.info("Shutting down Pak Trading Academy Bot...")

    await alert_engine.stop()
    await market.close()
    await whatsapp.close()

    logger.info("Shutdown complete.")
