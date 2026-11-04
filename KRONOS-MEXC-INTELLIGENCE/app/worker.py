from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import Job, MarketSnapshot, WorkerHeartbeat
from app.market.websocket import MexcTickerFeed
from app.services.jobs import (claim_job, enqueue_job, fail_job, finish_job,
                               recover_expired_jobs, renew_job_lease, worker_job_context)
from app.notifications.whatsapp_commands import notify_job_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kronos.worker")
STOP = False


def _stop(*_):
    global STOP
    STOP = True


def _last_scan_time():
    with SessionLocal() as db:
        value = db.scalar(select(Job.created_at).where(Job.job_type == "scan")
                          .order_by(Job.created_at.desc()).limit(1))
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _save_heartbeat(worker_id: str, role: str, status: str, current_job_id: str | None = None,
                    message: str = "Ready") -> None:
    with SessionLocal.begin() as db:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is None:
            row = WorkerHeartbeat(worker_id=worker_id, role=role, pid=os.getpid())
            db.add(row)
        row.role = role
        row.pid = os.getpid()
        row.status = status
        row.current_job_id = current_job_id
        row.message = message[:240]
        row.last_seen = datetime.now(timezone.utc)


def run(role: str | None = None):
    cfg = get_settings()
    role = role or cfg.worker_role
    init_db()
    log.info("[WORKER_BOOT] role=%s pid=%s", role, os.getpid())
    try:
        from app.services.pipeline import process_job
    except Exception:
        log.exception("[WORKER_IMPORT_FAILED] role=%s module=app.services.pipeline", role)
        raise
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{role}"
    recover_expired_jobs()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_recovery = time.monotonic()
    last_heartbeat = 0.0
    ws_feed = None
    ws_thread = None
    if role in {"market", "all"}:
        last_saved_by_symbol: dict[str, float] = {}

        async def persist_tickers(items: list[dict]):
            now_mono = time.monotonic()
            rows = []
            for item in items:
                symbol = str(item.get("symbol") or "").upper()
                try:
                    if not symbol or now_mono - last_saved_by_symbol.get(symbol, 0.0) < 300:
                        continue
                    last_saved_by_symbol[symbol] = now_mono
                    rows.append(MarketSnapshot(
                        symbol=symbol,
                        last_price=float(item["lastPrice"]) if item.get("lastPrice") is not None else None,
                        mark_price=float(item["fairPrice"]) if item.get("fairPrice") is not None else None,
                        index_price=float(item["indexPrice"]) if item.get("indexPrice") is not None else None,
                        volume_24h=float(item["volume24"]) if item.get("volume24") is not None else None,
                        turnover_24h=float(item["amount24"]) if item.get("amount24") is not None else None,
                    ))
                except (TypeError, ValueError):
                    continue
            if rows:
                try:
                    with SessionLocal.begin() as db:
                        db.add_all(rows)
                except Exception:
                    log.exception("MEXC WebSocket ticker snapshot persistence failed")

        ws_feed = MexcTickerFeed(on_tickers=persist_tickers)

        def websocket_runner():
            try:
                asyncio.run(ws_feed.run())
            except Exception:
                log.exception("MEXC WebSocket ticker feed stopped unexpectedly")

        ws_thread = threading.Thread(target=websocket_runner, name="mexc-ticker-websocket", daemon=True)
        ws_thread.start()
        log.info("MEXC ticker WebSocket feed started url=%s", cfg.mexc_ws_url)

    log.info("[WORKER_STARTED] id=%s role=%s auto_scan=%s database=%s",
             worker_id, role, cfg.auto_scan_enabled,
             "postgresql" if cfg.database_url.startswith("postgresql") else "sqlite")
    while not STOP:
        now_mono = time.monotonic()
        if now_mono - last_recovery >= 30:
            try:
                recover_expired_jobs()
            except Exception:
                log.exception("Expired-job recovery check failed")
            last_recovery = now_mono
        if now_mono - last_heartbeat >= 15:
            try:
                _save_heartbeat(worker_id, role, "ONLINE")
            except Exception:
                log.exception("Worker heartbeat write failed")
            last_heartbeat = now_mono
        if cfg.auto_scan_enabled and role in {"market", "all"}:
            last = _last_scan_time()
            if not last or (datetime.now(timezone.utc) - last).total_seconds() >= cfg.market_scan_interval_seconds:
                try:
                    enqueue_job("scan", {"scheduled": True})
                except Exception:
                    log.exception("Failed to enqueue scheduled market scan")
        job = claim_job(role, worker_id=worker_id)
        if job is None:
            time.sleep(cfg.worker_poll_seconds)
            continue
        request_id = str((job.payload or {}).get("request_id") or "")
        log.info("[WORKER_PICKED] worker=%s job_id=%s request_id=%s type=%s attempt=%s",
                 worker_id, job.id, request_id, job.job_type, job.attempts)
        log.info("[JOB_STARTED] job_id=%s request_id=%s type=%s", job.id, request_id, job.job_type)
        stop_lease = threading.Event()

        def keep_alive():
            while not stop_lease.wait(max(10, cfg.job_lease_seconds / 3)):
                try:
                    if not renew_job_lease(job.id, worker_id=worker_id):
                        log.warning("[JOB_LEASE_LOST] worker=%s job_id=%s", worker_id, job.id)
                        return
                    _save_heartbeat(worker_id, role, "BUSY", job.id, f"Processing {job.job_type}")
                except Exception:
                    log.exception("Job lease/heartbeat renewal failed id=%s", job.id)

        thread = threading.Thread(target=keep_alive, name=f"lease-{job.id[:8]}", daemon=True)
        thread.start()
        try:
            _save_heartbeat(worker_id, role, "BUSY", job.id, f"Processing {job.job_type}")
            with worker_job_context(worker_id):
                result = process_job(job)
                completed = finish_job(job.id, result)
            if not completed:
                log.warning("[JOB_COMPLETION_IGNORED] worker=%s job_id=%s reason=lease_or_owner_changed",
                            worker_id, job.id)
            else:
                log.info("[JOB_COMPLETED] worker=%s job_id=%s request_id=%s type=%s progress=100",
                         worker_id, job.id, request_id, job.job_type)
                try:
                    notify_job_completion(job, result=result)
                except Exception:
                    log.exception("WhatsApp completion notification failed id=%s", job.id)
        except Exception as exc:
            log.exception("[JOB_FAILED] worker=%s job_id=%s request_id=%s type=%s error_type=%s",
                          worker_id, job.id, request_id, job.job_type, type(exc).__name__)
            with worker_job_context(worker_id):
                accepted_failure = fail_job(job.id, f"{type(exc).__name__}: {exc}")
            if not accepted_failure:
                log.warning("[JOB_FAILURE_IGNORED] worker=%s job_id=%s reason=lease_or_owner_changed",
                            worker_id, job.id)
                continue
            # Do not tell the user a job is finally failed while a bounded retry is queued.
            if int(job.attempts or 0) >= int(job.max_attempts or 1):
                try:
                    notify_job_completion(job, error=f"{type(exc).__name__}: {exc}")
                except Exception:
                    log.exception("WhatsApp failure notification failed id=%s", job.id)
            else:
                log.info(
                    "job retry queued; suppressing final WhatsApp failure notice id=%s attempt=%s/%s",
                    job.id, job.attempts, job.max_attempts,
                )
        finally:
            stop_lease.set()
            thread.join(timeout=2)
            try:
                _save_heartbeat(worker_id, role, "ONLINE", message="Ready")
            except Exception:
                log.exception("Worker heartbeat reset failed")
    if ws_feed is not None:
        ws_feed.stop()
    if ws_thread is not None:
        ws_thread.join(timeout=5)
    log.info("[WORKER_STOPPED] id=%s role=%s graceful=true", worker_id, role)
    try:
        _save_heartbeat(worker_id, role, "OFFLINE", message="Graceful shutdown")
    except Exception:
        log.exception("Final worker heartbeat write failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["all", "market", "compute", "inference"], default=None)
    run(parser.parse_args().role)
