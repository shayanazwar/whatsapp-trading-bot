from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .alerts import AlertEngine
from .automation.executor import MexcExecutor
from .automation.mexc_client import MexcClient
from .automation.scanner import MexcScanner
from .automation.scheduler import ScannerScheduler
from .automation.signal_manager import SignalManager
from .automation.universe import MexcUniverse
from .backtest.runner import BacktestRunner
from .bot import Bot
from .charts import ChartRenderer
from .config import get_settings, validate_whatsapp_settings
from .database import Database
from .market import MarketData
from .whatsapp import WhatsAppClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("whatsapp_bot")

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await startup_event()
    try:
        yield
    finally:
        await shutdown_event()


app = FastAPI(title="Pak Trading Academy WhatsApp Trading Bot", lifespan=lifespan)

db = Database(settings.database_path)
whatsapp = WhatsAppClient(
    access_token=settings.meta_access_token,
    phone_number_id=settings.meta_phone_number_id,
    graph_version=settings.meta_graph_version,
    app_secret=settings.meta_app_secret,
)
charts = ChartRenderer(settings.chart_default_bars)
mexc_client = MexcClient(settings)
market = MarketData(settings, client=mexc_client)
bot = Bot(settings=settings, db=db, market=market, whatsapp=whatsapp, charts=charts)

alert_engine = AlertEngine(db=db, market=market, settings=settings, on_trigger=bot.send_triggered_alert)
mexc_universe = MexcUniverse(mexc_client, max_symbols=settings.max_symbols, test_symbols=settings.test_symbol_list)
signal_manager = SignalManager(db=db, whatsapp=whatsapp, recipients=settings.auto_signal_recipient_set, expiry_minutes=settings.signal_expiry_minutes)
mexc_executor = MexcExecutor(mexc_client, settings)
mexc_scanner = MexcScanner(client=mexc_client, settings=settings, universe=mexc_universe, signal_manager=signal_manager, executor=mexc_executor)
scanner_scheduler = ScannerScheduler(mexc_scanner, interval_seconds=settings.scan_interval_seconds)

# Share the process-wide MEXC client with live scanning so public API
# throttling, connection pooling, and response caches apply to both paths.
backtest_mexc_client = mexc_client
backtest_universe = MexcUniverse(backtest_mexc_client, max_symbols=settings.backtest_max_symbols, test_symbols=settings.test_symbol_list)
backtest_runner = BacktestRunner(client=backtest_mexc_client, universe=backtest_universe, settings=settings, max_concurrency=settings.backtest_symbol_concurrency)
bot.set_backtest_runner(backtest_runner)

_background_tasks: set[asyncio.Task] = set()
_processing_message_ids: set[str] = set()


def _retain_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    def done(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            logger.exception("Background task failed: %s", completed.get_name())
    task.add_done_callback(done)


def _configured() -> bool:
    return all(bool(str(v).strip()) for v in (
        settings.meta_access_token,
        settings.meta_phone_number_id,
        settings.meta_verify_token,
        settings.meta_app_secret,
    ))


@app.get("/")
async def root() -> dict[str, object]:
    return {
        "status": "online",
        "service": "Pak Trading Academy WhatsApp Trading Bot",
        "whatsapp_configured": _configured(),
        "scanner_enabled": settings.scanner_enabled,
        "auto_signal_enabled": settings.auto_signal_enabled,
        "auto_trade_enabled": settings.auto_trade_enabled,
        "live_execution_allowed": settings.allow_live_execution,
        "analysis_timeframes": ["1D", "12H", "4H", "1H"],
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    valid = (
        hub_mode == "subscribe"
        and bool(hub_verify_token)
        and bool(settings.meta_verify_token)
        and hmac.compare_digest(str(hub_verify_token), str(settings.meta_verify_token))
    )
    if valid:
        logger.info("WHATSAPP WEBHOOK VERIFICATION SUCCESS")
        return PlainTextResponse(hub_challenge or "", status_code=200)
    logger.warning("WHATSAPP WEBHOOK VERIFICATION FAILED")
    return PlainTextResponse("Forbidden", status_code=403)



async def process_webhook(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("WhatsApp webhook payload must be a JSON object")
    if payload.get("object") not in {None, "whatsapp_business_account"}:
        logger.warning("Ignoring unexpected webhook object=%s", payload.get("object"))
        return

    entries = payload.get("entry") or []
    if not isinstance(entries, list):
        raise ValueError("Webhook entry must be a list")

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                logger.warning("Ignoring malformed WhatsApp event value")
                continue
            metadata = value.get("metadata") or {}
            if not isinstance(metadata, dict):
                logger.warning("Ignoring malformed WhatsApp metadata")
                continue
            event_phone_number_id = str(metadata.get("phone_number_id") or "").strip()
            configured_phone_number_id = str(settings.meta_phone_number_id or "").strip()
            if configured_phone_number_id and event_phone_number_id and event_phone_number_id != configured_phone_number_id:
                logger.warning(
                    "Ignoring WhatsApp event for unexpected phone_number_id=%s expected=%s",
                    event_phone_number_id, configured_phone_number_id,
                )
                continue
            logger.info("WHATSAPP EVENT | field=messages phone_number_id=%s", event_phone_number_id or "MISSING")
            messages = value.get("messages") or []
            if not isinstance(messages, list):
                continue

            for message in messages:
                if not isinstance(message, dict):
                    logger.warning("Ignoring malformed WhatsApp message object")
                    continue
                message_id = str(message.get("id") or "")
                message_type = str(message.get("type") or "")
                sender = str(message.get("from") or "").strip()

                if message_id and message_id in _processing_message_ids:
                    logger.info("Duplicate in-flight WhatsApp message ignored | id=%s", message_id)
                    continue
                if message_id and db.is_message_seen(message_id):
                    logger.info("Duplicate processed WhatsApp message ignored | id=%s", message_id)
                    continue
                if message_id:
                    _processing_message_ids.add(message_id)

                try:
                    if message_type != "text":
                        logger.info("Ignoring non-text WhatsApp message | id=%s type=%s", message_id, message_type)
                        if message_id:
                            db.mark_message_seen(message_id)
                        continue
                    text = str(((message.get("text") or {}).get("body") or "")).strip()
                    if not sender or not text:
                        logger.warning("Ignoring text event missing sender/body | id=%s", message_id)
                        if message_id:
                            db.mark_message_seen(message_id)
                        continue

                    logger.info("WHATSAPP INBOUND | id=%s from=%s command=%s", message_id, sender, text.split()[0] if text.split() else "")
                    await bot.handle(sender, text)
                    if message_id:
                        db.mark_message_seen(message_id)
                    logger.info("WHATSAPP COMMAND COMPLETE | id=%s from=%s", message_id, sender)
                except Exception:
                    logger.exception("WHATSAPP COMMAND FAILED | id=%s from=%s", message_id, sender)
                finally:
                    if message_id:
                        _processing_message_ids.discard(message_id)


@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not whatsapp.verify_signature(body, signature):
        logger.warning("WHATSAPP WEBHOOK REJECTED | invalid signature")
        return JSONResponse({"status": "invalid_signature"}, status_code=403)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("WHATSAPP WEBHOOK REJECTED | invalid JSON")
        return JSONResponse({"status": "invalid_json"}, status_code=400)

    logger.info("WHATSAPP WEBHOOK RECEIVED | bytes=%d signature=valid", len(body))
    task = asyncio.create_task(process_webhook(payload), name="whatsapp-webhook")
    _retain_task(task)
    return JSONResponse({"status": "ok"}, status_code=200)


async def startup_event() -> None:
    validate_whatsapp_settings(settings)
    logger.info("Pak Trading Academy WhatsApp Trading Bot starting")
    logger.info("Graph API version=%s", settings.meta_graph_version)
    logger.info("MEXC API base=%s", settings.mexc_api_base_url)
    logger.info("Analysis timeframes=1D,12H,4H,1H")
    logger.info("Scanner enabled=%s auto_signals=%s auto_trade=%s live_execution=%s", settings.scanner_enabled, settings.auto_signal_enabled, settings.auto_trade_enabled, settings.allow_live_execution)
    await market.start()
    await alert_engine.start()
    if settings.scanner_enabled and settings.auto_signal_enabled:
        await scanner_scheduler.start()
    logger.info("Bot startup complete")


async def shutdown_event() -> None:
    logger.info("Shutting down Pak Trading Academy WhatsApp Trading Bot")
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
    logger.info("Shutdown complete")
