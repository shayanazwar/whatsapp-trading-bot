"""FastAPI webhook/health entry point.

Long-running command processing runs in background tasks.
Live trading is intentionally disabled.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
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
    WhatsAppCloud,
    WhatsAppPolicyBlocked,
    extract_incoming_messages,
    extract_status_updates,
    sender_key,
    verify_webhook_signature,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

MAX_WEBHOOK_BYTES = 1_000_000


def _allowed_senders(settings: Settings) -> set[str]:
    return {
        key
        for value in settings.whatsapp_allowed_senders
        if (key := sender_key(value))
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(cfg.database_path)
        mexc = MexcClient(
            base_url=cfg.mexc_rest_base_url,
            timeout_seconds=cfg.http_timeout_seconds,
            max_retries=cfg.http_max_retries,
        )
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
            app.state.scheduler_task = asyncio.create_task(
                scheduler.run_forever(),
                name="research-scheduler",
            )
        else:
            store.set_state("scheduler_status", "disabled")

        # Recover queued inbound commands only when the trading-message
        # policy gate is open and the sender is explicitly allowlisted.
        if cfg.whatsapp_policy_gate_open:
            allowed = _allowed_senders(cfg)

            if allowed:
                for row in store.pending_inbound(limit=50):
                    try:
                        payload = json.loads(row["payload_json"])
                        messages = extract_incoming_messages(payload)
                        message = next(
                            (
                                item
                                for item in messages
                                if item["message_id"] == row["message_id"]
                            ),
                            None,
                        )

                        if message and message["from"] in allowed:
                            _spawn_message_task(
                                app,
                                message,
                                row["message_id"],
                            )
                    except Exception:
                        logger.warning(
                            "Could not recover one pending inbound message"
                        )

        logger.info(
            "Application started | policy_gate=%s | live_execution=false",
            "open" if cfg.whatsapp_policy_gate_open else "closed",
        )

        yield

        scheduler.stop()

        scheduler_task = app.state.scheduler_task
        if scheduler_task is not None:
            scheduler_task.cancel()
            await asyncio.gather(
                scheduler_task,
                return_exceptions=True,
            )

        tasks = list(app.state.background_tasks)
        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await mexc.aclose()
        await whatsapp.aclose()

    app = FastAPI(
        title="MEXC Futures Research Bot",
        version="0.2.1",
        lifespan=lifespan,
    )

    @app.api_route("/", methods=["GET", "HEAD"])
    async def root() -> dict[str, Any]:
        return {
            "service": "MEXC Futures Research Bot",
            "mode": "research/paper",
            "live_execution": "disabled",
            "health": "/healthz",
            "health_legacy": "/health",
            "whatsapp_webhook": "/webhook/whatsapp",
            "whatsapp_webhook_legacy": "/webhook",
        }

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    @app.api_route("/health", methods=["GET", "HEAD"])
    async def healthz() -> dict[str, Any]:
        store: Store = app.state.store
        settings: Settings = app.state.settings

        try:
            store.recent_events(1)
            database_status = "ok"
        except Exception:
            database_status = "error"

        return {
            "status": "ok" if database_status == "ok" else "degraded",
            "database": database_status,
            "scheduler_enabled": settings.scheduler_enabled,
            "scheduler_worker": store.get_state(
                "scheduler_status",
                "not_started",
            ),
            "scheduler_last_success": store.get_state(
                "scheduler_last_success"
            ),
            "paper_scheduler_gate": store.get_state(
                "paper_scheduler_gate",
                "not_evaluated",
            ),
            "paper_trading_enabled": settings.paper_trading_enabled,
            "data_alignment_probe_approved": (
                settings.data_alignment_probe_approved
            ),
            "auto_trading_enabled": settings.auto_trading_enabled,
            "live_execution_enabled": False,
            "live_execution_build_released": (
                settings.live_execution_build_released
            ),
            "whatsapp_policy_gate": (
                "open-by-configuration"
                if settings.whatsapp_policy_gate_open
                else "blocked"
            ),
        }

    @app.get("/webhook", response_class=PlainTextResponse)
    @app.get("/webhook/whatsapp", response_class=PlainTextResponse)
    async def whatsapp_verify(
        hub_mode: str | None = Query(None, alias="hub.mode"),
        hub_verify_token: str | None = Query(
            None,
            alias="hub.verify_token",
        ),
        hub_challenge: str | None = Query(
            None,
            alias="hub.challenge",
        ),
    ) -> PlainTextResponse:
        settings: Settings = app.state.settings

        valid = (
            hub_mode == "subscribe"
            and bool(settings.meta_verify_token)
            and bool(hub_verify_token)
            and hmac.compare_digest(
                settings.meta_verify_token,
                hub_verify_token,
            )
            and hub_challenge is not None
        )

        if valid:
            return PlainTextResponse(hub_challenge, status_code=200)

        raise HTTPException(
            status_code=403,
            detail="Webhook verification failed",
        )

    @app.post("/webhook")
    @app.post("/webhook/whatsapp")
    async def whatsapp_inbound(
        request: Request,
    ) -> dict[str, str]:
        settings: Settings = app.state.settings
        store: Store = app.state.store

        raw_body = await request.body()

        if len(raw_body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Payload too large",
            )

        signature = request.headers.get("X-Hub-Signature-256")

        if not verify_webhook_signature(
            raw_body,
            signature,
            settings.meta_app_secret,
        ):
            raise HTTPException(
                status_code=403,
                detail="Webhook signature invalid",
            )

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail="Malformed JSON",
            ) from exc

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail="Payload must be an object",
            )

        messages = extract_incoming_messages(payload)
        status_updates = extract_status_updates(payload)

        for update in status_updates:
            provider_id = update.get("provider_message_id", "")
            status = update.get("status", "")

            if provider_id and status:
                store.record_outbox_status(
                    provider_id,
                    status,
                    update.get("error"),
                    details={
                        "recipient_id": update.get("recipient_id"),
                        "timestamp": update.get("timestamp"),
                        "pricing": update.get("pricing", {}),
                    },
                )

        allowed = _allowed_senders(settings)

        for message in messages:
            message_id = message.get("message_id", "")
            sender = message.get("from", "")

            if (
                settings.meta_phone_number_id
                and message.get("phone_number_id")
                and message["phone_number_id"]
                != settings.meta_phone_number_id
            ):
                store.append_event(
                    "WHATSAPP_WRONG_PHONE_NUMBER_ID",
                    {
                        "message_id": message_id,
                        "received_phone_number_id": (
                            message["phone_number_id"]
                        ),
                    },
                    idempotency_key=f"wrong-phone-id:{message_id}",
                )
                continue

            if not message_id or not sender:
                store.append_event(
                    "WHATSAPP_INVALID_INBOUND",
                    {"reason": "missing id/sender"},
                )
                continue

            accepted = store.save_inbound(
                message_id,
                sender,
                message.get("body", ""),
                payload,
            )

            if not accepted:
                store.append_event(
                    "WHATSAPP_DUPLICATE_INBOUND",
                    {"message_id": message_id},
                    idempotency_key=f"duplicate-inbound:{message_id}",
                )
                continue

            # Always enforce the allowlist before sending anything.
            if not allowed or sender not in allowed:
                store.mark_inbound_processed(
                    message_id,
                    "unauthorized_sender_dropped",
                )
                store.append_event(
                    "WHATSAPP_UNAUTHORIZED_SENDER",
                    {"message_id": message_id},
                    idempotency_key=f"unauthorized:{message_id}",
                )
                continue

            # A closed gate never executes PRICE, BACKTEST, or any other
            # crypto/trading command. It only schedules the generic
            # service notice, which contains no market/trading information.
            if not settings.whatsapp_policy_gate_open:
                store.append_event(
                    "WHATSAPP_POLICY_GATE_BLOCKED",
                    {
                        "message_id": message_id,
                        "reply_type": "generic_service_status_only",
                    },
                    idempotency_key=f"policy-blocked:{message_id}",
                )
                _spawn_policy_status_task(app, message, message_id)
                continue

            _spawn_message_task(app, message, message_id)

        return {"status": "accepted"}

    return app


def _spawn_task(app: FastAPI, coroutine, *, name: str) -> None:
    task = asyncio.create_task(coroutine, name=name)
    app.state.background_tasks.add(task)

    def finished(done: asyncio.Task) -> None:
        app.state.background_tasks.discard(done)

        if done.cancelled():
            return

        try:
            done.result()
        except Exception as exc:
            logger.error(
                "Background task failed | task=%s | error=%s",
                name,
                type(exc).__name__,
            )

    task.add_done_callback(finished)


def _spawn_message_task(
    app: FastAPI,
    message: dict[str, str],
    message_id: str,
) -> None:
    _spawn_task(
        app,
        _process_one_message(app, message, message_id),
        name=f"whatsapp-command-{message_id}",
    )


def _spawn_policy_status_task(
    app: FastAPI,
    message: dict[str, str],
    message_id: str,
) -> None:
    _spawn_task(
        app,
        _send_policy_status_reply(app, message, message_id),
        name=f"whatsapp-status-{message_id}",
    )


async def _send_policy_status_reply(
    app: FastAPI,
    message: dict[str, str],
    message_id: str,
) -> None:
    """Send only the fixed non-trading acknowledgement."""
    store: Store = app.state.store
    sender = message.get("from", "")
    outbox_id = None

    response_key = f"wa-policy-status:{message_id}"

    try:
        outbox_id = store.enqueue_outbox(
            sender,
            (
                "Thanks for your message. Automated responses are "
                "temporarily unavailable. Please try again later."
            ),
            category="service",
            idempotency_key=response_key,
        )

        result = await app.state.whatsapp.send_service_status(sender)
        provider_messages = result.get("messages", [])
        provider_id = (
            provider_messages[0].get("id")
            if provider_messages
            and isinstance(provider_messages[0], dict)
            else None
        )

        store.record_outbox_attempt(
            outbox_id,
            accepted=True,
            provider_message_id=provider_id,
        )
        store.append_event(
            "WHATSAPP_SERVICE_STATUS_ACCEPTED_BY_META",
            {
                "message_id": message_id,
                "provider_message_id": provider_id,
                "outbox_id": outbox_id,
                "delivery_confirmed": False,
            },
            idempotency_key=f"wa-service-status-accepted:{message_id}",
        )
        store.mark_inbound_processed(
            message_id,
            "policy_gate_closed_service_status_sent",
        )

    except Exception as exc:
        if outbox_id is not None:
            try:
                store.mark_outbox_unknown(
                    outbox_id,
                    type(exc).__name__,
                )
            except Exception:
                logger.warning(
                    "Could not update service-status outbox record"
                )

        store.append_event(
            "WHATSAPP_SERVICE_STATUS_FAILED",
            {
                "message_id": message_id,
                "error_type": type(exc).__name__,
            },
            idempotency_key=f"wa-service-status-failed:{message_id}",
        )
        store.mark_inbound_processed(
            message_id,
            f"service_status_failed:{type(exc).__name__}",
        )
        logger.warning(
            "Generic service reply failed | message_id=%s | error=%s",
            message_id,
            type(exc).__name__,
        )


async def _process_one_message(
    app: FastAPI,
    message: dict[str, str],
    message_id: str,
) -> None:
    store: Store = app.state.store
    settings: Settings = app.state.settings
    processor: CommandProcessor = app.state.processor
    sender = message.get("from", "")
    body = message.get("body", "")

    try:
        if message.get("type") != "text" or not body.strip():
            store.mark_inbound_processed(
                message_id,
                "unsupported_message_type",
            )
            return

        try:
            message_time = int(message.get("timestamp", ""))
        except (TypeError, ValueError):
            message_time = None

        result = await processor.process_text(
            sender,
            body,
            received_timestamp=message_time,
            enforce_whatsapp_policy=True,
        )

        response_key = f"wa-reply:{message_id}:text"
        outbox_id = store.enqueue_outbox(
            sender,
            result.text,
            category="service",
            idempotency_key=response_key,
        )

        try:
            provider_result = await app.state.whatsapp.send_text(
                sender,
                result.text,
            )
            provider_messages = provider_result.get("messages", [])
            provider_id = (
                provider_messages[0].get("id")
                if provider_messages
                and isinstance(provider_messages[0], dict)
                else None
            )

            store.record_outbox_attempt(
                outbox_id,
                accepted=True,
                provider_message_id=provider_id,
            )
            store.append_event(
                "WHATSAPP_MESSAGE_ACCEPTED_BY_META",
                {
                    "recipient": sender,
                    "provider_message_id": provider_id,
                    "outbox_id": outbox_id,
                    "category": "service",
                    "delivery_confirmed": False,
                },
                idempotency_key=f"wa-accepted:{response_key}",
            )

        except Exception as exc:
            # A send timeout can occur after Meta has accepted a message.
            # Record unknown and do not blindly retry it.
            store.mark_outbox_unknown(
                outbox_id,
                type(exc).__name__,
            )
            store.append_event(
                "WHATSAPP_SEND_UNKNOWN",
                {
                    "outbox_id": outbox_id,
                    "error_type": type(exc).__name__,
                },
                idempotency_key=f"wa-send-unknown:{response_key}",
            )
            store.mark_inbound_processed(
                message_id,
                "reply_outcome_unknown_no_retry",
            )
            return

        for path, media_type in result.attachments:
            media_key = f"wa-reply:{message_id}:{media_type}:{path}"
            media_outbox_id = store.enqueue_outbox(
                sender,
                f"[{media_type.upper()}] {Path(path).name}",
                category="service",
                idempotency_key=media_key,
            )

            try:
                media_result = await app.state.whatsapp.send_media(
                    sender,
                    path,
                    media_type=media_type,
                )
                media_messages = media_result.get("messages", [])
                media_provider_id = (
                    media_messages[0].get("id")
                    if media_messages
                    and isinstance(media_messages[0], dict)
                    else None
                )
                store.record_outbox_attempt(
                    media_outbox_id,
                    accepted=True,
                    provider_message_id=media_provider_id,
                )
                store.append_event(
                    "WHATSAPP_MEDIA_ACCEPTED_BY_META",
                    {
                        "message_id": message_id,
                        "media_type": media_type,
                        "provider_message_id": media_provider_id,
                        "outbox_id": media_outbox_id,
                        "delivery_confirmed": False,
                    },
                    idempotency_key=(
                        f"wa-media-accepted:{message_id}:"
                        f"{media_type}:{path}"
                    ),
                )
            except Exception as exc:
                store.mark_outbox_unknown(
                    media_outbox_id,
                    type(exc).__name__,
                )
                store.append_event(
                    "WHATSAPP_MEDIA_SEND_UNKNOWN",
                    {
                        "message_id": message_id,
                        "media_type": media_type,
                        "error_type": type(exc).__name__,
                    },
                    idempotency_key=(
                        f"wa-media-unknown:{message_id}:{path}"
                    ),
                )

        store.mark_inbound_processed(message_id, "completed")

    except WhatsAppPolicyBlocked:
        store.mark_inbound_processed(
            message_id,
            "crypto_policy_gate_closed",
        )
    except Exception as exc:
        store.append_event(
            "WHATSAPP_COMMAND_FAILED",
            {
                "message_id": message_id,
                "error_type": type(exc).__name__,
            },
            idempotency_key=f"wa-command-failed:{message_id}",
        )
        store.mark_inbound_processed(
            message_id,
            f"failed:{type(exc).__name__}",
        )
        logger.warning(
            "WhatsApp command failed | message_id=%s | error=%s",
            message_id,
            type(exc).__name__,
        )


app = create_app()
