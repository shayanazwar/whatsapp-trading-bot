from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import suppress
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .alerts import AlertEngine
from .automation.executor import MexcExecutor
from .automation.mexc_client import MexcClient
from .automation.scanner import MexcScanner
from .automation.scheduler import ScannerScheduler
from .automation.signal_manager import SignalManager
from .automation.universe import MexcUniverse
from .bot import Bot
from .charts import ChartRenderer
from .config import get_settings
from .database import Database
from .market import MarketData
from .whatsapp import WhatsAppClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("whatsapp_bot")

settings = get_settings()
app = FastAPI(title="Pak Trading Academy WhatsApp Bot")

db = Database(settings.database_path)
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

mexc_client = MexcClient(settings)
mexc_universe = MexcUniverse(
    mexc_client,
    max_symbols=settings.max_symbols,
    test_symbols=settings.test_symbol_list,
)
signal_manager = SignalManager(
    db=db,
    whatsapp=whatsapp,
    recipients=settings.auto_signal_recipient_set,
    expiry_minutes=settings.signal_expiry_minutes,
)
mexc_executor = MexcExecutor(mexc_client, settings)
mexc_scanner = MexcScanner(
    client=mexc_client,
    settings=settings,
    universe=mexc_universe,
    signal_manager=signal_manager,
    executor=mexc_executor,
)
scanner_scheduler = ScannerScheduler(
    mexc_scanner,
    interval_seconds=settings.scan_interval_seconds,
)

_background_tasks: set[asyncio.Task] = set()


def keep_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _validate_runtime_config() -> None:
    required = {
        "META_PHONE_NUMBER_ID": settings.meta_phone_number_id,
        "META_ACCESS_TOKEN": settings.meta_access_token,
        "META_APP_SECRET": settings.meta_app_secret,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError("Missing required configuration: " + ", ".join(missing))


@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Pak Trading Academy WhatsApp Bot",
        "scanner_enabled": settings.scanner_enabled,
        "auto_signal_enabled": settings.auto_signal_enabled,
        "auto_trade_enabled": settings.auto_trade_enabled,
        "live_execution_allowed": settings.allow_live_execution,
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/webhook")
async def verify_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
):
    if (
        hub_mode == "subscribe"
        and hub_verify_token
        and settings.meta_verify_token
        and hmac.compare_digest(hub_verify_token, settings.meta_verify_token)
    ):
        logger.info("WEBHOOK VERIFICATION SUCCESS")
        return PlainTextResponse(content=hub_challenge or "", status_code=200)

    logger.warning("WEBHOOK VERIFICATION FAILED")
    return PlainTextResponse(content="Forbidden", status_code=403)


async def process_webhook(payload: dict) -> None:
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

                if message_id and not db.mark_message_seen(message_id):
                    logger.info("Ignoring duplicate message: %s", message_id)
                    continue

                if message_type != "text":
                    continue

                text = message.get("text", {}).get("body", "").strip()
                if not sender or not text:
                    continue

                logger.info("Incoming text: %s", text)
                try:
                    await bot.handle(sender, text)
                except Exception:
                    logger.exception("Bot command processing failed")


@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not whatsapp.verify_signature(body, signature):
        logger.warning("Webhook signature verification FAILED")
        return JSONResponse(content={"status": "invalid_signature"}, status_code=403)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(content={"status": "invalid_json"}, status_code=400)

    task = asyncio.create_task(process_webhook(payload), name="whatsapp-webhook")
    keep_task(task)
    return JSONResponse(content={"status": "ok"}, status_code=200)


@app.on_event("startup")
async def startup_event():
    _validate_runtime_config()

    logger.info("==========================================")
    logger.info("Pak Trading Academy WhatsApp Bot starting")
    logger.info("==========================================")
    logger.info("Graph API version: %s", settings.meta_graph_version)
    logger.info("MEXC API base: %s", settings.mexc_api_base_url)
    logger.info("Scanner enabled: %s", settings.scanner_enabled)
    logger.info("Auto signals enabled: %s", settings.auto_signal_enabled)
    logger.info("Auto trade enabled: %s", settings.auto_trade_enabled)
    logger.info("Live execution allowed: %s", settings.allow_live_execution)

    await market.start()
    await alert_engine.start()

    if settings.scanner_enabled:
        await scanner_scheduler.start()

    logger.info("Bot startup complete.")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Pak Trading Academy Bot...")
    with suppress(Exception):
        await scanner_scheduler.stop()
    with suppress(Exception):
        await alert_engine.stop()
    with suppress(Exception):
        await market.close()
    with suppress(Exception):
        await mexc_client.close()
    with suppress(Exception):
        await whatsapp.close()
    logger.info("Shutdown complete.")
