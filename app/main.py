"""FastAPI health API, optional scanner/Telegram commands, and Meta WhatsApp webhook endpoints."""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.logging_config import configure_logging
from app.persistence.store import SQLiteStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, store: SQLiteStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    settings.ensure_database_parent()
    storage = store or SQLiteStore(settings.database_path)

    market_data_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        configure_logging(settings.log_level)
        logger.info(
            "SHAYAN AI API starting; scanner_enabled=%s whatsapp_enabled=%s telegram_enabled=%s telegram_commands_enabled=%s",
            settings.enable_scanner, settings.enable_whatsapp, settings.enable_telegram,
            settings.enable_telegram and settings.enable_telegram_commands,
        )
        background_tasks: list[asyncio.Task] = []
        if settings.enable_scanner:
            from app.services.scanner import scanner_loop
            background_tasks.append(asyncio.create_task(
                scanner_loop(settings, storage, market_data_lock), name="shayan-ai-scanner",
            ))
        if settings.enable_telegram and settings.enable_telegram_commands:
            from app.services.telegram_commands import telegram_command_loop
            background_tasks.append(asyncio.create_task(
                telegram_command_loop(settings, storage, market_data_lock), name="shayan-ai-telegram-commands",
            ))
        try:
            yield
        finally:
            for task in background_tasks:
                task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            logger.info("SHAYAN AI API stopped")

    app = FastAPI(title="SHAYAN AI", version="0.4.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = storage
    app.state.market_data_lock = market_data_lock

    async def read_bounded_body(request: Request) -> bytes:
        max_bytes = settings.webhook_max_body_bytes
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
            if declared_length < 0:
                raise HTTPException(status_code=400, detail="Invalid Content-Length header")
            if declared_length > max_bytes:
                raise HTTPException(status_code=413, detail="Webhook body exceeds configured size limit")
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > max_bytes:
                raise HTTPException(status_code=413, detail="Webhook body exceeds configured size limit")
            chunks.extend(chunk)
        return bytes(chunks)

    @app.get("/")
    async def root() -> dict[str, object]:
        return {
            "service": "SHAYAN AI",
            "version": "0.4.0",
            "health": "/health",
            "live_order_execution": False,
        }

    @app.get("/health")
    async def health() -> dict[str, object]:
        # SQLite is synchronous; run it in a thread so lock waits do not block the ASGI event loop.
        pending_notifications = await run_in_threadpool(storage.count_pending_outbox)
        return {
            "status": "ok", "service": "SHAYAN AI", "version": "0.4.0",
            "scanner_enabled": settings.enable_scanner,
            "whatsapp_enabled": settings.enable_whatsapp,
            "telegram_enabled": settings.enable_telegram,
            "telegram_commands_enabled": settings.enable_telegram and settings.enable_telegram_commands,
            "pending_notifications": pending_notifications,
            "live_order_execution": False,
        }

    @app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
    async def verify_webhook(request: Request) -> Response:
        mode = request.query_params.get("hub.mode", "")
        verify_token = request.query_params.get("hub.verify_token", "")
        challenge = request.query_params.get("hub.challenge", "")
        if mode == "subscribe" and settings.meta_verify_token and hmac.compare_digest(verify_token, settings.meta_verify_token) and challenge:
            return PlainTextResponse(challenge, status_code=200)
        raise HTTPException(status_code=403, detail="Webhook verification failed")

    @app.post("/webhooks/whatsapp")
    async def receive_webhook(request: Request) -> dict[str, str]:
        if not settings.meta_app_secret:
            raise HTTPException(status_code=503, detail="Webhook signature verification is not configured")
        raw = await read_bounded_body(request)
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not signature.startswith("sha256="):
            raise HTTPException(status_code=401, detail="Missing valid webhook signature")
        expected = "sha256=" + hmac.new(settings.meta_app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="Webhook signature mismatch")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Malformed JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected JSON object")

        body_hash = hashlib.sha256(raw).hexdigest()
        status_events = 0
        entries = payload.get("entry", [])
        if isinstance(entries, list):
            for entry in entries:
                changes = entry.get("changes", []) if isinstance(entry, dict) else []
                if not isinstance(changes, list):
                    continue
                for change in changes:
                    value = change.get("value", {}) if isinstance(change, dict) else {}
                    statuses = value.get("statuses", []) if isinstance(value, dict) else []
                    if not isinstance(statuses, list):
                        continue
                    for event in statuses:
                        if not isinstance(event, dict):
                            continue
                        message_id = str(event.get("id", ""))
                        status = str(event.get("status", ""))
                        event_time = str(event.get("timestamp", ""))
                        if not message_id or not status:
                            continue
                        event_key = f"{message_id}:{status}:{event_time}"
                        event_result = await run_in_threadpool(
                            storage.apply_webhook_status_event,
                            event_key,
                            message_id,
                            status,
                            body_hash,
                            event_time or None,
                        )
                        if event_result in {"applied", "deferred"}:
                            status_events += 1
        # Do not log the webhook body, recipient list, or message content.
        logger.info("WhatsApp webhook accepted; status_events=%d", status_events)
        return {"status": "received"}

    return app


app = create_app()
