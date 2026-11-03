from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import socket
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Job, SystemEvent


def enqueue_job(job_type: str, payload: dict | None = None, *, max_attempts: int | None = None) -> Job:
    payload = payload or {}
    cfg = get_settings()
    with SessionLocal() as db:
        # Idempotency for currently active equivalent jobs.
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        active = db.scalars(select(Job).where(
            Job.job_type == job_type,
            Job.status.in_(["queued", "running"]),
        ).order_by(Job.created_at.desc()).limit(100)).all()
        for existing in active:
            if json.dumps(existing.payload or {}, sort_keys=True, separators=(",", ":")) == encoded:
                return existing
        job = Job(job_type=job_type, payload=payload, max_attempts=max_attempts or cfg.job_max_attempts)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


def claim_job(role: str = "all") -> Job | None:
    role_types = {
        "market": ["scan"],
        "compute": ["analysis", "forecast", "backtest", "evaluate_forecast"],
        "inference": ["analysis", "forecast", "backtest", "evaluate_forecast"],
        "all": ["scan", "analysis", "forecast", "backtest", "evaluate_forecast"],
    }
    allowed = role_types.get(role, role_types["all"])
    worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        statement = select(Job).where(
            Job.status == "queued", Job.job_type.in_(allowed)
        ).order_by(Job.created_at.asc()).limit(1).with_for_update(skip_locked=True)
        job = db.scalars(statement).first()
        if not job:
            return None
        job.status = "running"
        job.started_at = now
        job.lease_expires_at = now + timedelta(seconds=get_settings().job_lease_seconds)
        job.worker_id = worker_id
        job.attempts += 1
        job.progress = max(float(job.progress or 0), 0.01)
        job.progress_message = "Claimed by worker"
        db.flush()
        db.expunge(job)
        return job


def update_progress(job_id: str, progress: float, message: str):
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if not job:
            return
        job.progress = max(0.0, min(100.0, float(progress)))
        job.progress_message = message[:240]
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().job_lease_seconds)


def finish_job(job_id: str, result: dict):
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job:
            job.status = "completed"
            job.progress = 100
            job.progress_message = "Completed"
            job.result = result
            job.error = None
            job.finished_at = datetime.now(timezone.utc)
            job.lease_expires_at = None
            db.add(SystemEvent(level="INFO", event_type="JOB_COMPLETED", message=f"{job.job_type} completed",
                               details={"job_id": job_id}))


def fail_job(job_id: str, error: str):
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if not job:
            return
        job.error = error[:4000]
        job.lease_expires_at = None
        job.finished_at = datetime.now(timezone.utc)
        if job.attempts < job.max_attempts:
            job.status = "queued"
            job.progress_message = f"Retry scheduled ({job.attempts}/{job.max_attempts})"
        else:
            job.status = "failed"
            job.progress_message = "Failed after bounded retries"
        db.add(SystemEvent(level="ERROR", event_type="JOB_FAILED", message=f"{job.job_type}: {error[:400]}",
                           details={"job_id": job_id, "attempt": job.attempts}))


def recover_expired_jobs():
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        jobs = db.scalars(select(Job).where(Job.status == "running", Job.lease_expires_at < now)).all()
        for job in jobs:
            job.status = "queued" if job.attempts < job.max_attempts else "failed"
            job.error = "Worker lease expired; job recovered by watchdog"
            job.lease_expires_at = None
            job.worker_id = None


def job_to_dict(job: Job) -> dict:
    return {
        "id": job.id, "type": job.job_type, "status": job.status,
        "progress": job.progress, "progress_message": job.progress_message,
        "result": job.result, "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def renew_job_lease(job_id: str) -> None:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if job and job.status == "running":
            job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().job_lease_seconds)
