from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.backtest import run_backtest
from app.commands import HELP_TEXT
from app.config import settings
from app.database import Database
from app.mexc import MexcClient
from app.monitor import MarketMonitor
from app.whatsapp import WhatsAppClient, extract_inbound_messages, split_backtest_command

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    db = Database(settings.database_path)
    mexc = MexcClient(settings)
    whatsapp = WhatsAppClient(settings)
    app.state.settings = settings
    app.state.db = db
    app.state.mexc = mexc
    app.state.whatsapp = whatsapp
    app.state.backtest_lock = asyncio.Lock()
    app.state.jobs: set[asyncio.Task[Any]] = set()
    app.state.monitor_task = None
    if settings.monitor_enabled:
        if not (settings.wa_phone_number_id and settings.wa_access_token and settings.wa_app_secret and settings.wa_recipients):
            log.warning("Monitoring is enabled but WhatsApp credentials/recipients are incomplete; signals cannot be delivered")
        monitor = MarketMonitor(settings, db, mexc, whatsapp)
        app.state.monitor_task = asyncio.create_task(monitor.run_forever(), name="btc-market-monitor")
    log.info("Service started; order execution capability is not included")
    try:
        yield
    finally:
        task = app.state.monitor_task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for job in list(app.state.jobs):
            job.cancel()
        if app.state.jobs:
            await asyncio.gather(*app.state.jobs, return_exceptions=True)
        await mexc.aclose()
        await whatsapp.aclose()
        log.info("Service stopped")


app = FastAPI(title="BTCUSDT MEXC Signal & Backtest", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "BTCUSDT MEXC Signal & Backtest", "status": "ok", "execution": "disabled"}


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    configured = bool(settings.wa_phone_number_id and settings.wa_access_token and settings.wa_verify_token and settings.wa_app_secret)
    return {
        "status": "ok",
        "market": "BTC_USDT",
        "timeframe": "Min1",
        "monitor_enabled": settings.monitor_enabled,
        "whatsapp_configured": configured,
        "signal_recipients_configured": bool(settings.wa_recipients),
        "order_execution": False,
    }


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(request: Request) -> PlainTextResponse:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")
    if mode == "subscribe" and settings.wa_verify_token and token and token == settings.wa_verify_token:
        return PlainTextResponse(challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


async def _send_reply(app_obj: FastAPI, recipient: str, message: str) -> None:
    await app_obj.state.whatsapp.send_chunks(recipient, message)


async def _run_backtest_job(app_obj: FastAPI, recipient: str, days: int, direction: str) -> None:
    async with app_obj.state.backtest_lock:
        try:
            result = await run_backtest(app_obj.state.mexc, settings, days, direction, int(time.time()))
            await _send_reply(app_obj, recipient, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Backtest job failed: %s", type(exc).__name__)
            try:
                await _send_reply(app_obj, recipient, "Backtest failed because market data or service processing failed. No fabricated results were returned.")
            except Exception as send_exc:
                log.error("Backtest failure notification could not be delivered (%s)", type(send_exc).__name__)


@app.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, str]:
    body_parts: list[bytes] = []
    body_size = 0
    async for part in request.stream():
        body_size += len(part)
        if body_size > 1_000_000:
            raise HTTPException(status_code=413, detail="Payload too large")
        body_parts.append(part)
    body = b"".join(body_parts)
    if not app.state.whatsapp.verify_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    now = int(time.time())
    for message in extract_inbound_messages(payload):
        sender = message["sender"]
        if not app.state.db.mark_message_processed(message["message_id"], now):
            continue
        if sender not in settings.authorized_wa_ids:
            continue  # silently ignore senders not on the explicit allowlist
        command = message["body"].strip()
        lowered = command.lower()
        if lowered in {"/help", "help"}:
            try:
                await _send_reply(app, sender, HELP_TEXT)
            except Exception as exc:
                log.error("Help reply failed (%s)", type(exc).__name__)
        elif lowered in {"/status", "status"}:
            paused = app.state.db.get("abnormal_paused", "0") == "1"
            condition = app.state.db.get("monitor_last_condition", "UNKNOWN")
            last_candle = app.state.db.get("monitor_last_candle", "UNKNOWN")
            status = (
                "BTCUSDT BOT STATUS\n"
                f"Monitor: {'ON' if settings.monitor_enabled else 'OFF'}\n"
                f"Abnormal-movement pause: {'ON' if paused else 'OFF'}\n"
                f"Last signal condition: {condition}\n"
                f"Last processed candle (UTC Unix): {last_candle}\n"
                "Live execution: DISABLED\nPaper orders: NOT USED"
            )
            try:
                await _send_reply(app, sender, status)
            except Exception as exc:
                log.error("Status reply failed (%s)", type(exc).__name__)
        else:
            parsed = split_backtest_command(command)
            if parsed:
                days, direction = parsed
                if app.state.backtest_lock.locked():
                    try:
                        await _send_reply(app, sender, "A backtest is already running. Please try again after it finishes.")
                    except Exception as exc:
                        log.error("Busy reply failed (%s)", type(exc).__name__)
                else:
                    task = asyncio.create_task(_run_backtest_job(app, sender, days, direction))
                    app.state.jobs.add(task)
                    task.add_done_callback(app.state.jobs.discard)
                    try:
                        await _send_reply(app, sender, f"Backtest queued: {days}D / {direction}. Fresh MEXC candles will be downloaded. Results will follow in WhatsApp.")
                    except Exception as exc:
                        log.error("Backtest queued reply failed (%s)", type(exc).__name__)
            else:
                try:
                    await _send_reply(app, sender, "Unknown command. Send /help to see available commands.")
                except Exception as exc:
                    log.error("Invalid-command reply failed (%s)", type(exc).__name__)
    return {"status": "received"}
