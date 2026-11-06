from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from rslr.config import settings
from rslr.jobs import process_backtest_jobs
from rslr.storage import Database
from rslr.whatsapp import process_webhook_payload, verify_webhook_signature

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
log = logging.getLogger(__name__)


def _job_runner(stop_event: threading.Event) -> None:
    """Poll the persistent backtest-job queue (10s cadence).

    Runs inside the web process because the deployed topology has no
    separate worker service; the claim is atomic, so a future worker
    process can safely run the same loop without double-execution.
    """
    while not stop_event.is_set():
        try:
            processed = process_backtest_jobs(database)
            if processed:
                log.info("Processed %s backtest jobs", processed)
        except Exception:
            log.exception("Backtest job runner cycle failed")
        stop_event.wait(10)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info(
        "RSLR web starting up (whatsapp configured: verify_token=%s app_secret=%s allowed_senders=%s)",
        bool(settings.whatsapp_verify_token),
        bool(settings.whatsapp_app_secret),
        len(settings.whatsapp_allowed_numbers),
    )
    stop_event = threading.Event()
    app.state.job_runner_stop = stop_event
    thread = threading.Thread(
        target=_job_runner, args=(stop_event,), daemon=True, name="rslr-backtest-jobs"
    )
    thread.start()
    app.state.job_runner = thread
    yield
    stop_event.set()
    log.info("RSLR web shutting down")


app = FastAPI(title="RSLR Trading Bot", version="0.1.0", lifespan=lifespan)
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
    request: Request,
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> str:
    if hub_mode == "subscribe" and settings.whatsapp_verify_token and hub_verify_token == settings.whatsapp_verify_token:
        log.info("Webhook verification accepted: path=%s", request.url.path)
        return hub_challenge or ""
    log.warning("Webhook verification failed: path=%s mode=%s", request.url.path, hub_mode)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook/whatsapp")
@app.post("/webhook")
async def whatsapp_webhook(request: Request) -> dict[str, bool]:
    raw = await request.body()
    if not verify_webhook_signature(raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("Webhook signature invalid: path=%s", request.url.path)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("Webhook invalid JSON: path=%s", request.url.path)
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    entries = len(payload.get("entry", [])) if isinstance(payload, dict) else 0
    log.info("Webhook accepted: path=%s entries=%s", request.url.path, entries)
    await run_in_threadpool(process_webhook_payload, payload, database)
    return {"accepted": True}
