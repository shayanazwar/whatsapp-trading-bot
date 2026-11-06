from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from rslr.config import settings
from rslr.storage import Database
from rslr.whatsapp import process_webhook_payload, verify_webhook_signature

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
log = logging.getLogger(__name__)
app = FastAPI(title="RSLR Trading Bot", version="0.1.0")
database = Database()


@app.get("/health")
def health() -> dict[str, str | bool]:
    try:
        with database.engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        db_ok = True
    except Exception:
        log.exception("Database health check failed")
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "live_execution": False,
        "paper_trading": settings.paper_trading_enabled,
    }


@app.get("/webhook/whatsapp", response_class=PlainTextResponse)
@app.get("/webhook", response_class=PlainTextResponse)
def verify_whatsapp_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> str:
    if hub_mode == "subscribe" and settings.whatsapp_verify_token and hub_verify_token == settings.whatsapp_verify_token:
        return hub_challenge or ""
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook/whatsapp")
@app.post("/webhook")
async def whatsapp_webhook(request: Request) -> dict[str, bool]:
    raw = await request.body()
    if not verify_webhook_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    await run_in_threadpool(process_webhook_payload, payload, database)
    return {"accepted": True}
