"""FastAPI webhook/health entry point. Long-running command processing is backgrounded."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.config import Settings, load_settings
from app.mexc.client import MexcClient
from app.services.command_processor import CommandProcessor
from app.services.scheduler import ResearchScheduler
from app.storage import Store
from app.whatsapp import (
    WhatsAppCloud, WhatsAppPolicyBlocked, extract_incoming_messages, extract_status_updates,
    sender_key, verify_webhook_signature,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(cfg.database_path)
        mexc = MexcClient(base_url=cfg.mexc_rest_base_url, timeout_seconds=cfg.http_timeout_seconds,
                          max_retries=cfg.http_max_retries)
        whatsapp = WhatsAppCloud(cfg)
        processor = CommandProcessor(cfg, store, mexc, whatsapp)
        scheduler = ResearchScheduler(cfg, store, mexc, whatsapp)
        app.state.settings = cfg
        app.state.store = store
        app.state.mexc = mexc
        app.state.whatsapp = whatsapp
        app.state.processor = processor
        app.state.scheduler = scheduler
        app.state.scheduler_task = None
        app.state.background_tasks = set()
        if cfg.scheduler_enabled:
            app.state.scheduler_task = asyncio.create_task(scheduler.run_forever(), name="research-scheduler")
        else:
            store.set_state("scheduler_status", "disabled")
        # Recover durably stored but unprocessed webhooks only when the policy gate and allowlist are open.
        if cfg.whatsapp_policy_gate_open and cfg.whatsapp_allowed_senders:
            for row in store.pending_inbound(limit=50):
                try:
                    payload = json.loads(row["payload_json"])
                    msgs = extract_incoming_messages(payload)
                    msg = next((m for m in msgs if m["message_id"] == row["message_id"]), None)
                    if msg:
                        _spawn_message_task(app, msg, row["message_id"])
                except Exception:
                    logger.warning("Could not requeue one stored inbound webhook row")
        yield
        scheduler.stop()
        scheduler_task = app.state.scheduler_task
        if scheduler_task is not None:
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
        tasks = list(app.state.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await mexc.aclose()
        await whatsapp.aclose()

    app = FastAPI(title="MEXC Futures Research Bot", version="0.2.0", lifespan=lifespan)

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {"service": "MEXC Futures Research Bot", "mode": "research/paper", "live_execution": "disabled",
                "health": "/healthz", "whatsapp_webhook": "/webhook/whatsapp"}

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        store: Store = app.state.store
        try:
            store.recent_events(1)
            db = "ok"
        except Exception:
            db = "error"
        settings: Settings = app.state.settings
        return {
            "status": "ok" if db == "ok" else "degraded",
            "database": db,
            "scheduler_enabled": settings.scheduler_enabled,
            "scheduler_worker": store.get_state("scheduler_status", "not_started"),
            "scheduler_last_success": store.get_state("scheduler_last_success"),
            "paper_scheduler_gate": store.get_state("paper_scheduler_gate", "not_evaluated"),
            "paper_trading_enabled": settings.paper_trading_enabled,
            "data_alignment_probe_approved": settings.data_alignment_probe_approved,
            "auto_trading_enabled": settings.auto_trading_enabled,
            "live_execution_enabled": False,
            "live_execution_build_released": settings.live_execution_build_released,
            "whatsapp_policy_gate": "open-by-configuration" if settings.whatsapp_policy_gate_open else "blocked",
        }

    @app.get("/webhook/whatsapp", response_class=PlainTextResponse)
    async def whatsapp_verify(
        hub_mode: str | None = Query(None, alias="hub.mode"),
        hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(None, alias="hub.challenge"),
    ) -> PlainTextResponse:
        settings: Settings = app.state.settings
        if hub_mode == "subscribe" and settings.meta_verify_token and hub_verify_token and hmac.compare_digest(
            settings.meta_verify_token, hub_verify_token
        ) and hub_challenge is not None:
            return PlainTextResponse(hub_challenge, status_code=200)
        raise HTTPException(status_code=403, detail="Webhook verification failed")

    @app.post("/webhook/whatsapp")
    async def whatsapp_inbound(request: Request) -> dict[str, str]:
        settings: Settings = app.state.settings
        raw_body = await request.body()
        if len(raw_body) > 1_000_000:
            raise HTTPException(status_code=413, detail="Payload too large")
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_webhook_signature(raw_body, signature, settings.meta_app_secret):
            raise HTTPException(status_code=403, detail="Webhook signature invalid")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Malformed JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be an object")
        messages = extract_incoming_messages(payload)
        status_updates = extract_status_updates(payload)
        store: Store = app.state.store
        for update in status_updates:
            provider_id = update.get("provider_message_id", "")
            status = update.get("status", "")
            if provider_id and status:
                store.record_outbox_status(
                    provider_id, status, update.get("error"),
                    details={
                        "recipient_id": update.get("recipient_id"),
                        "timestamp": update.get("timestamp"),
                        "pricing": update.get("pricing", {}),
                    },
                )
        for msg in messages:
            if (settings.meta_phone_number_id and msg.get("phone_number_id")
                    and msg["phone_number_id"] != settings.meta_phone_number_id):
                store.append_event("WHATSAPP_WRONG_PHONE_NUMBER_ID",
                    {"message_id": msg.get("message_id", ""), "received_phone_number_id": msg["phone_number_id"]},
                    idempotency_key=f"wrong-phone-id:{msg.get('message_id', '')}")
                continue
            if not msg["message_id"] or not msg["from"]:
                store.append_event("WHATSAPP_INVALID_INBOUND", {"reason": "missing id/sender"})
                continue
            accepted = store.save_inbound(msg["message_id"], msg["from"], msg["body"], payload)
            if not accepted:
                store.append_event("WHATSAPP_DUPLICATE_INBOUND", {"message_id": msg["message_id"]},
                                   idempotency_key=f"duplicate-inbound:{msg['message_id']}")
                continue
            if not settings.whatsapp_policy_gate_open:
                store.mark_inbound_processed(msg["message_id"], "crypto_policy_gate_closed; no command executed")
                store.append_event("WHATSAPP_POLICY_GATE_BLOCKED", {"message_id": msg["message_id"], "sender": msg["from"]},
                                   idempotency_key=f"policy-blocked:{msg['message_id']}")
                # Deliberately no reply; don't disguise trading content as a generic status message.
                continue
            allowed = {sender_key(x) for x in settings.whatsapp_allowed_senders}
            if not allowed or msg["from"] not in allowed:
                store.mark_inbound_processed(msg["message_id"], "unauthorized_sender_dropped")
                store.append_event("WHATSAPP_UNAUTHORIZED_SENDER", {"message_id": msg["message_id"]},
                                   idempotency_key=f"unauthorized:{msg['message_id']}")
                continue
            _spawn_message_task(app, msg, msg["message_id"])
        # Persist all useful information first and acknowledge quickly; no strategy work happens inline.
        return {"status": "accepted"}

    return app


def _spawn_message_task(app: FastAPI, message: dict[str, str], message_id: str) -> None:
    task = asyncio.create_task(_process_one_message(app, message, message_id))
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)


async def _process_one_message(app: FastAPI, message: dict[str, str], message_id: str) -> None:
    store: Store = app.state.store
    settings: Settings = app.state.settings
    processor: CommandProcessor = app.state.processor
    sender = message.get("from", "")
    body = message.get("body", "")
    try:
        if message.get("type") != "text" or not body.strip():
            store.mark_inbound_processed(message_id, "unsupported_message_type")
            return
        timestamp_raw = message.get("timestamp", "")
        try:
            message_time = int(timestamp_raw)
        except (TypeError, ValueError):
            message_time = None
        result = await processor.process_text(sender, body, received_timestamp=message_time, enforce_whatsapp_policy=True)
        response_key = f"wa-reply:{message_id}:text"
        outbox_id = store.enqueue_outbox(sender, result.text, category="service", idempotency_key=response_key)
        try:
            provider_result = await app.state.whatsapp.send_text(sender, result.text)
            provider_messages = provider_result.get("messages", [])
            provider_id = provider_messages[0].get("id") if provider_messages and isinstance(provider_messages[0], dict) else None
            store.record_outbox_attempt(outbox_id, accepted=True, provider_message_id=provider_id)
            store.append_event("WHATSAPP_MESSAGE_ACCEPTED_BY_META", {"recipient": sender, "provider_message_id": provider_id,
                "outbox_id": outbox_id, "category": "service", "delivery_confirmed": False},
                idempotency_key=f"wa-accepted:{response_key}")
        except Exception as exc:
            # A timeout may have happened after Meta accepted the message; mark unknown and do not blind retry.
            store.mark_outbox_unknown(outbox_id, type(exc).__name__)
            store.append_event("WHATSAPP_SEND_UNKNOWN", {"outbox_id": outbox_id, "error_type": type(exc).__name__},
                               idempotency_key=f"wa-send-unknown:{response_key}")
            store.mark_inbound_processed(message_id, "reply_outcome_unknown_no_retry")
            return
        for path, media_type in result.attachments:
            media_key = f"wa-reply:{message_id}:{media_type}:{path}"
            media_outbox_id = store.enqueue_outbox(sender, f"[{media_type.upper()}] {Path(path).name}",
                category="service", idempotency_key=media_key)
            try:
                media_result = await app.state.whatsapp.send_media(sender, path, media_type=media_type)
                media_messages = media_result.get("messages", [])
                media_provider_id = media_messages[0].get("id") if media_messages and isinstance(media_messages[0], dict) else None
                store.record_outbox_attempt(media_outbox_id, accepted=True, provider_message_id=media_provider_id)
                store.append_event("WHATSAPP_MEDIA_ACCEPTED_BY_META", {"message_id": message_id,
                    "media_type": media_type, "provider_message_id": media_provider_id, "outbox_id": media_outbox_id,
                    "delivery_confirmed": False}, idempotency_key=f"wa-media-accepted:{message_id}:{media_type}:{path}")
            except Exception as exc:
                store.mark_outbox_unknown(media_outbox_id, type(exc).__name__)
                store.append_event("WHATSAPP_MEDIA_SEND_UNKNOWN", {"message_id": message_id, "media_type": media_type,
                    "error_type": type(exc).__name__}, idempotency_key=f"wa-media-unknown:{message_id}:{path}")
        store.mark_inbound_processed(message_id, "completed")
    except WhatsAppPolicyBlocked:
        store.mark_inbound_processed(message_id, "crypto_policy_gate_closed")
    except Exception as exc:
        store.append_event("WHATSAPP_COMMAND_FAILED", {"message_id": message_id, "error_type": type(exc).__name__},
                           idempotency_key=f"wa-command-failed:{message_id}")
        store.mark_inbound_processed(message_id, f"failed:{type(exc).__name__}")
        logger.warning("WhatsApp command processing failed (%s); message content not logged", type(exc).__name__)


app = create_app()
