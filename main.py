import os
import json
import hmac
import hashlib
import asyncio
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse

# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("whatsapp_bot")

# ---------------------------------------------------------
# ENVIRONMENT
# ---------------------------------------------------------

META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")

# Your production WhatsApp Phone Number ID
WHATSAPP_PHONE_NUMBER_ID = os.getenv(
    "WHATSAPP_PHONE_NUMBER_ID",
    "1285387147997440",
)

# Meta Graph API version
META_GRAPH_VERSION = os.getenv(
    "META_GRAPH_VERSION",
    "v26.0",
)

# ---------------------------------------------------------
# FASTAPI
# ---------------------------------------------------------

app = FastAPI(title="Pak Trading Academy WhatsApp Bot")

# Keep references to background tasks so they cannot disappear
_background_tasks = set()


def _keep_task(task: asyncio.Task):
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
    return {
        "status": "healthy",
    }


# ---------------------------------------------------------
# WEBHOOK VERIFICATION
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
    logger.info("========== WEBHOOK VERIFICATION ==========")
    logger.info("hub.mode=%s", hub_mode)
    logger.info("hub.verify_token_received=%s", bool(hub_verify_token))
    logger.info("hub.challenge_received=%s", bool(hub_challenge))

    if (
        hub_mode == "subscribe"
        and hub_verify_token
        and hmac.compare_digest(
            hub_verify_token,
            META_VERIFY_TOKEN,
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
# SIGNATURE VERIFICATION
# ---------------------------------------------------------

def verify_meta_signature(
    body: bytes,
    signature: Optional[str],
) -> bool:

    # If App Secret isn't configured, reject production requests.
    if not META_APP_SECRET:
        logger.error(
            "META_APP_SECRET is missing. "
            "Cannot verify Meta webhook signature."
        )
        return False

    if not signature:
        logger.warning(
            "POST /webhook arrived without X-Hub-Signature-256"
        )
        return False

    expected = (
        "sha256="
        + hmac.new(
            META_APP_SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
    )

    return hmac.compare_digest(
        expected,
        signature,
    )


# ---------------------------------------------------------
# WHATSAPP SEND MESSAGE
# ---------------------------------------------------------

async def send_whatsapp_text(
    recipient: str,
    message: str,
) -> dict:

    if not META_ACCESS_TOKEN:
        raise RuntimeError("META_ACCESS_TOKEN is not configured")

    if not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError(
            "WHATSAPP_PHONE_NUMBER_ID is not configured"
        )

    url = (
        f"https://graph.facebook.com/"
        f"{META_GRAPH_VERSION}/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {
            "body": message,
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            headers=headers,
            json=payload,
        )

    logger.info(
        "WhatsApp send response: HTTP %s",
        response.status_code,
    )

    if response.status_code >= 400:
        logger.error(
            "WhatsApp API error: %s",
            response.text,
        )

    response.raise_for_status()

    return response.json()


# ---------------------------------------------------------
# PROCESS INCOMING WHATSAPP EVENT
# ---------------------------------------------------------

async def process_webhook(payload: dict):

    logger.info("========== PROCESSING WHATSAPP EVENT ==========")

    logger.info(
        "Webhook object=%s",
        payload.get("object"),
    )

    entries = payload.get("entry", [])

    if not entries:
        logger.info("Webhook contains no entries.")
        return

    for entry in entries:

        changes = entry.get("changes", [])

        for change in changes:

            field = change.get("field")
            value = change.get("value", {})

            logger.info(
                "Webhook field=%s",
                field,
            )

            # We only need WhatsApp message events here.
            if field != "messages":
                logger.info(
                    "Ignoring non-message webhook field: %s",
                    field,
                )
                continue

            messages = value.get("messages", [])

            if not messages:
                logger.info(
                    "Message webhook contains no messages "
                    "(possibly a status event)."
                )
                continue

            for message in messages:

                message_id = message.get("id")
                message_type = message.get("type")
                sender = message.get("from")

                logger.info(
                    "Incoming WhatsApp message: "
                    "id=%s type=%s from=%s",
                    message_id,
                    message_type,
                    sender,
                )

                # -------------------------------------------------
                # TEXT MESSAGE
                # -------------------------------------------------

                if message_type == "text":

                    text_data = message.get("text", {})
                    incoming_text = text_data.get("body", "")

                    logger.info(
                        "Incoming text: %s",
                        incoming_text,
                    )

                    # TEMPORARY TEST RESPONSE.
                    #
                    # Once webhook delivery is confirmed, this can
                    # be replaced with your existing Bot handler.
                    try:
                        await send_whatsapp_text(
                            sender,
                            f"✅ Webhook received!\n\n"
                            f"You said: {incoming_text}",
                        )

                        logger.info(
                            "Webhook test reply sent successfully."
                        )

                    except Exception:
                        logger.exception(
                            "Failed to send WhatsApp reply."
                        )

                else:
                    logger.info(
                        "Ignoring unsupported message type: %s",
                        message_type,
                    )


# ---------------------------------------------------------
# WEBHOOK POST
# ---------------------------------------------------------

@app.post("/webhook")
async def receive_webhook(request: Request):

    # ---------------------------------------------------------
    # CRITICAL DIAGNOSTIC LOG
    # ---------------------------------------------------------
    #
    # This MUST appear in Render if Meta sends us anything.
    #
    logger.info("🔥🔥🔥 WEBHOOK POST RECEIVED 🔥🔥🔥")

    # Read the raw body first.
    body = await request.body()

    logger.info(
        "Webhook body size=%d bytes",
        len(body),
    )

    signature = request.headers.get(
        "X-Hub-Signature-256"
    )

    logger.info(
        "X-Hub-Signature-256 present=%s",
        bool(signature),
    )

    # ---------------------------------------------------------
    # VERIFY META SIGNATURE
    # ---------------------------------------------------------

    if not verify_meta_signature(
        body,
        signature,
    ):
        logger.warning(
            "Webhook signature verification FAILED"
        )

        return JSONResponse(
            content={
                "status": "invalid_signature",
            },
            status_code=403,
        )

    logger.info(
        "Webhook signature verification PASSED"
    )

    # ---------------------------------------------------------
    # PARSE JSON
    # ---------------------------------------------------------

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.exception(
            "Webhook body was not valid JSON."
        )

        return JSONResponse(
            content={
                "status": "invalid_json",
            },
            status_code=400,
        )

    logger.info(
        "Webhook JSON parsed successfully."
    )

    logger.info(
        "Webhook object=%s",
        payload.get("object"),
    )

    # ---------------------------------------------------------
    # ACK META IMMEDIATELY
    # ---------------------------------------------------------

    # Meta should receive a successful response quickly.
    # The actual processing happens in the background.

    task = asyncio.create_task(
        process_webhook(payload)
    )

    _keep_task(task)

    logger.info(
        "Webhook acknowledged; processing task started."
    )

    return JSONResponse(
        content={
            "status": "ok",
        },
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
        "WhatsApp Phone Number ID: %s",
        WHATSAPP_PHONE_NUMBER_ID,
    )

    logger.info(
        "Meta Graph API version: %s",
        META_GRAPH_VERSION,
    )

    logger.info(
        "META_VERIFY_TOKEN configured: %s",
        bool(META_VERIFY_TOKEN),
    )

    logger.info(
        "META_APP_SECRET configured: %s",
        bool(META_APP_SECRET),
    )

    logger.info(
        "META_ACCESS_TOKEN configured: %s",
        bool(META_ACCESS_TOKEN),
    )

    logger.info(
        "Webhook endpoint: /webhook"
    )

    logger.info(
        "=========================================="
    )


# ---------------------------------------------------------
# SHUTDOWN
# ---------------------------------------------------------

@app.on_event("shutdown")
async def shutdown_event():

    logger.info(
        "Pak Trading Academy WhatsApp Bot shutting down."
    )
