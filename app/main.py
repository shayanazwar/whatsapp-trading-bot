from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
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
from .backtest.runner import BacktestRunner
from .bot import Bot
from .charts import ChartRenderer
from .config import get_settings
from .database import Database
from .market import MarketData
from .telegram import TelegramClient
from .whatsapp import WhatsAppClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("whatsapp_bot")

# IMPORTANT: no synthetic BACKTEST/ANALYZE WhatsApp keepalive task exists in this canonical runtime.
# Do not reintroduce a periodic message loop; backtests must remain user-triggered.


settings = get_settings()

app = FastAPI(
    title="Pak Trading Academy Dual-Channel Trading Bot"
)


# ============================================================
# CORE SERVICES
# ============================================================

db = Database(settings.database_path)

whatsapp = WhatsAppClient(
    access_token=settings.meta_access_token,
    phone_number_id=settings.meta_phone_number_id,
    graph_version=settings.meta_graph_version,
    app_secret=settings.meta_app_secret,
)

telegram = TelegramClient(settings.telegram_bot_token)

charts = ChartRenderer(
    settings.chart_default_bars
)


# ============================================================
# MEXC MARKET DATA
# IMPORTANT:
# mexc_client and market MUST be created BEFORE Bot/AlertEngine
# because they depend on the market object.
# ============================================================

mexc_client = MexcClient(settings)

market = MarketData(
    settings,
    client=mexc_client,
)


# ============================================================
# BOT
# ============================================================

bot = Bot(
    settings=settings,
    db=db,
    market=market,
    whatsapp=whatsapp,
    telegram=telegram,
    charts=charts,
)


# ============================================================
# ALERT ENGINE
# ============================================================

alert_engine = AlertEngine(
    db=db,
    market=market,
    settings=settings,
    on_trigger=bot.send_triggered_alert,
)


# ============================================================
# MEXC AUTOMATION
# ============================================================

mexc_universe = MexcUniverse(
    mexc_client,
    max_symbols=settings.max_symbols,
    test_symbols=settings.test_symbol_list,
)

signal_manager = SignalManager(
    db=db,
    whatsapp=whatsapp,
    telegram=telegram,
    recipients=settings.auto_signal_recipient_set,
    expiry_minutes=settings.signal_expiry_minutes,
)

mexc_executor = MexcExecutor(
    mexc_client,
    settings,
)

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


# ============================================================
# HISTORICAL BACKTEST
# IMPORTANT:
# This is paper simulation only. It never uses the executor.
# ============================================================

backtest_mexc_client = MexcClient(settings)

backtest_universe = MexcUniverse(
    backtest_mexc_client,
    max_symbols=settings.max_symbols,
    test_symbols=settings.test_symbol_list,
)

backtest_runner = BacktestRunner(
    client=backtest_mexc_client,
    universe=backtest_universe,
    settings=settings,
    max_concurrency=4,
)

bot.set_backtest_runner(backtest_runner)


# ============================================================
# BACKGROUND TASK MANAGEMENT
# ============================================================

_background_tasks: set[asyncio.Task] = set()


def keep_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ============================================================
# CONFIG VALIDATION
# ============================================================

def _validate_runtime_config() -> None:
    whatsapp_ready = bool(
        settings.meta_phone_number_id
        and settings.meta_access_token
        and settings.meta_app_secret
    )
    telegram_ready = bool(settings.telegram_bot_token)

    if not whatsapp_ready and not telegram_ready:
        raise RuntimeError(
            "No messaging channel is configured. Set WhatsApp credentials "
            "or TELEGRAM_BOT_TOKEN."
        )

    logger.info(
        "Messaging channels configured: WhatsApp=%s Telegram=%s",
        whatsapp_ready,
        telegram_ready,
    )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Pak Trading Academy Dual-Channel Trading Bot",
        "whatsapp_configured": bool(
            settings.meta_phone_number_id
            and settings.meta_access_token
            and settings.meta_app_secret
        ),
        "telegram_configured": bool(settings.telegram_bot_token),
        "scanner_enabled": settings.scanner_enabled,
        "auto_signal_enabled": settings.auto_signal_enabled,
        "auto_trade_enabled": settings.auto_trade_enabled,
        "live_execution_allowed": settings.allow_live_execution,
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "healthy"
    }


# ============================================================
# WHATSAPP WEBHOOK VERIFICATION
# ============================================================

@app.get("/webhook")
async def verify_webhook(
    hub_mode: Optional[str] = Query(
        None,
        alias="hub.mode",
    ),
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
        logger.info(
            "WEBHOOK VERIFICATION SUCCESS"
        )

        return PlainTextResponse(
            content=hub_challenge or "",
            status_code=200,
        )

    logger.warning(
        "WEBHOOK VERIFICATION FAILED"
    )

    return PlainTextResponse(
        content="Forbidden",
        status_code=403,
    )


# ============================================================
# PROCESS WHATSAPP WEBHOOK
# ============================================================

async def process_webhook(payload: dict) -> None:
    for entry in payload.get("entry", []):

        for change in entry.get("changes", []):

            if change.get("field") != "messages":
                continue

            value = change.get(
                "value",
                {},
            )

            for message in value.get(
                "messages",
                [],
            ):

                message_id = message.get("id")
                message_type = message.get("type")
                sender = message.get("from")

                logger.info(
                    "Incoming message: id=%s type=%s from=%s",
                    message_id,
                    message_type,
                    sender,
                )

                if (
                    message_id
                    and not db.mark_message_seen(
                        message_id
                    )
                ):
                    logger.info(
                        "Ignoring duplicate message: %s",
                        message_id,
                    )
                    continue

                if message_type != "text":
                    continue

                text = (
                    message
                    .get("text", {})
                    .get("body", "")
                    .strip()
                )

                if not sender or not text:
                    continue

                logger.info(
                    "Incoming text: %s",
                    text,
                )

                try:
                    await bot.handle(
                        sender,
                        text,
                    )

                except Exception:
                    logger.exception(
                        "Bot command processing failed"
                    )


# ============================================================
# WHATSAPP WEBHOOK RECEIVER
# ============================================================

@app.post("/webhook")
async def receive_webhook(
    request: Request,
):
    body = await request.body()

    signature = request.headers.get(
        "X-Hub-Signature-256"
    )

    if not whatsapp.verify_signature(
        body,
        signature,
    ):
        logger.warning(
            "Webhook signature verification FAILED"
        )

        return JSONResponse(
            content={
                "status": "invalid_signature"
            },
            status_code=403,
        )

    try:
        payload = json.loads(body)

    except json.JSONDecodeError:

        return JSONResponse(
            content={
                "status": "invalid_json"
            },
            status_code=400,
        )

    task = asyncio.create_task(
        process_webhook(payload),
        name="whatsapp-webhook",
    )

    keep_task(task)

    return JSONResponse(
        content={
            "status": "ok"
        },
        status_code=200,
    )



# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

async def process_telegram_update(payload: dict) -> None:
    message = payload.get("message") or payload.get("edited_message")
    if not message:
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    text = (message.get("text") or "").strip()
    if chat_id is None or not text:
        return

    target = f"tg:{chat_id}"
    dedupe_id = f"telegram:{chat_id}:{message_id}" if message_id is not None else None

    logger.info(
        "Incoming Telegram message: update_id=%s message_id=%s chat_id=%s",
        payload.get("update_id"),
        message_id,
        chat_id,
    )

    if dedupe_id and not db.mark_message_seen(dedupe_id):
        logger.info("Ignoring duplicate Telegram message: %s", dedupe_id)
        return

    try:
        await bot.handle(target, text)
    except Exception:
        logger.exception("Telegram bot command processing failed")


@app.post("/telegram/webhook")
async def receive_telegram_webhook(request: Request):
    if not settings.telegram_bot_token:
        return JSONResponse(
            content={"status": "telegram_not_configured"},
            status_code=503,
        )

    expected = settings.telegram_webhook_secret
    if expected:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(provided, expected):
            logger.warning("Telegram webhook secret verification FAILED")
            return JSONResponse(
                content={"status": "invalid_secret"},
                status_code=403,
            )

    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(
            content={"status": "invalid_json"},
            status_code=400,
        )

    task = asyncio.create_task(
        process_telegram_update(payload),
        name="telegram-webhook",
    )
    keep_task(task)

    return JSONResponse(content={"status": "ok"}, status_code=200)

# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():

    _validate_runtime_config()

    logger.info(
        "=========================================="
    )

    logger.info(
        "Pak Trading Academy Dual-Channel Trading Bot starting"
    )

    logger.info(
        "=========================================="
    )

    logger.info(
        "Graph API version: %s",
        settings.meta_graph_version,
    )

    logger.info(
        "MEXC API base: %s",
        settings.mexc_api_base_url,
    )

    logger.info(
        "Scanner enabled: %s",
        settings.scanner_enabled,
    )

    logger.info(
        "Auto signals enabled: %s",
        settings.auto_signal_enabled,
    )

    logger.info(
        "Auto trade enabled: %s",
        settings.auto_trade_enabled,
    )

    logger.info(
        "Live execution allowed: %s",
        settings.allow_live_execution,
    )

    # Start market data
    await market.start()

    # Start normal alert engine
    await alert_engine.start()

    # Start MEXC scanner
    if settings.scanner_enabled:
        await scanner_scheduler.start()

    if telegram.configured:
        with suppress(Exception):
            await telegram.set_my_commands()

        if settings.telegram_webhook_url:
            await telegram.set_webhook(
                settings.telegram_webhook_url,
                secret_token=settings.telegram_webhook_secret,
                drop_pending_updates=False,
            )
            logger.info("Telegram webhook configured: %s", settings.telegram_webhook_url)

    logger.info("Bot startup complete.")


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown_event():

    logger.info(
        "Shutting down Pak Trading Academy Bot..."
    )

    with suppress(Exception):
        await scanner_scheduler.stop()

    with suppress(Exception):
        await alert_engine.stop()

    with suppress(Exception):
        await market.close()

    with suppress(Exception):
        await mexc_client.close()

    with suppress(Exception):
        await backtest_mexc_client.close()

    with suppress(Exception):
        await whatsapp.close()

    with suppress(Exception):
        await telegram.close()

    logger.info(
        "Shutdown complete."
    )
