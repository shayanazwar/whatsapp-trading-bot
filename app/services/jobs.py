from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from contextvars import ContextVar
import json
import socket
import uuid

from sqlalchemy import and_, case, or_, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Job, SystemEvent

_ACTIVE_WORKER_ID: ContextVar[str] = ContextVar("kronos_active_worker_id", default="")


@contextmanager
def worker_job_context(worker_id: str):
    """Bind progress/result mutations to the worker currently executing a job."""
    token = _ACTIVE_WORKER_ID.set(worker_id)
    try:
        yield
    finally:
        _ACTIVE_WORKER_ID.reset(token)


def _owns_job(job: Job) -> bool:
    worker_id = _ACTIVE_WORKER_ID.get()
    return not worker_id or job.worker_id == worker_id


def enqueue_job(job_type: str, payload: dict | None = None, *, max_attempts: int | None = None) -> Job:
    payload = payload or {}
    cfg = get_settings()
    with SessionLocal() as db:
        # Idempotency for currently active equivalent jobs.
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        active = db.scalars(select(Job).where(
            Job.job_type == job_type,
            Job.status.in_(["queued", "running"]),
        ).order_by(Job.created_at.desc())).all()
        for existing in active:
            if json.dumps(existing.payload or {}, sort_keys=True, separators=(",", ":")) == encoded:
                return existing
        job = Job(job_type=job_type, payload=payload, max_attempts=max_attempts or cfg.job_max_attempts)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


def claim_job(role: str = "all", *, worker_id: str | None = None) -> Job | None:
    role_types = {
        "market": ["scan"],
        # Forecasting/analysis jobs cannot be queued behind long historical tests.
        "inference": ["analysis", "forecast", "evaluate_forecast"],
        "compute": ["backtest"],
        "all": ["scan", "analysis", "forecast", "backtest", "evaluate_forecast"],
    }
    if role not in role_types:
        raise ValueError(f"Unsupported worker role: {role}")
    allowed = role_types[role]
    worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        priority = case(
            (Job.job_type == "analysis", 0),
            (Job.job_type == "forecast", 1),
            (Job.job_type == "evaluate_forecast", 2),
            (Job.job_type == "backtest", 3),
            (Job.job_type == "scan", 4),
            else_=5,
        )
        statement = select(Job).where(
            Job.status == "queued", Job.job_type.in_(allowed),
            or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
        ).order_by(priority.asc(), Job.created_at.asc()).limit(1).with_for_update(skip_locked=True)
        job = db.scalars(statement).first()
        if not job:
            return None
        job.status = "running"
        job.started_at = now
        job.lease_expires_at = now + timedelta(seconds=get_settings().job_lease_seconds)
        job.worker_id = worker_id
        job.attempts += 1
        job.next_attempt_at = None
        job.progress = max(float(job.progress or 0), 0.01)
        job.progress_message = "Claimed by worker"
        db.flush()
        db.expunge(job)
        return job


def update_progress(job_id: str, progress: float, message: str):
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if not job:
            raise KeyError(f"Job {job_id} no longer exists")
        if job.status != "running" or not _owns_job(job):
            raise RuntimeError(f"Worker no longer owns running job {job_id}; refusing stale progress update")
        job.progress = max(0.0, min(100.0, float(progress)))
        job.progress_message = message[:240]
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().job_lease_seconds)


def finish_job(job_id: str, result: dict) -> bool:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if not job or job.status != "running" or not _owns_job(job):
            return False
        job.status = "completed"
        job.progress = 100
        job.progress_message = "Completed"
        job.result = result
        job.error = None
        job.finished_at = datetime.now(timezone.utc)
        job.lease_expires_at = None
        job.next_attempt_at = None
        db.add(SystemEvent(level="INFO", event_type="JOB_COMPLETED", message=f"{job.job_type} completed",
                           details={"job_id": job_id, "worker_id": job.worker_id}))
        return True


def fail_job(job_id: str, error: str) -> bool:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if not job or job.status != "running" or not _owns_job(job):
            return False
        job.error = error[:4000]
        job.lease_expires_at = None
        now = datetime.now(timezone.utc)
        if job.attempts < job.max_attempts:
            job.status = "queued"
            delay_seconds = min(15 * (2 ** max(job.attempts - 1, 0)), 900)
            job.next_attempt_at = now + timedelta(seconds=delay_seconds)
            job.finished_at = None
            job.progress_message = (
                f"Retry scheduled in {delay_seconds}s ({job.attempts}/{job.max_attempts})"
            )
        else:
            job.status = "failed"
            job.next_attempt_at = None
            job.finished_at = now
            job.progress_message = "Failed after bounded retries"
        db.add(SystemEvent(level="ERROR", event_type="JOB_FAILED", message=f"{job.job_type}: {error[:400]}",
                           details={"job_id": job_id, "attempt": job.attempts, "worker_id": job.worker_id}))
        return True


def recover_expired_jobs():
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        jobs = db.scalars(select(Job).where(
            Job.status == "running",
            or_(
                Job.lease_expires_at < now,
                and_(
                    Job.lease_expires_at.is_(None),
                    Job.started_at.is_not(None),
                    Job.started_at < now - timedelta(seconds=get_settings().job_lease_seconds),
                ),
            ),
        ).with_for_update(skip_locked=True)).all()
        for job in jobs:
            if job.attempts < job.max_attempts:
                job.status = "queued"
                delay_seconds = min(15 * (2 ** max(job.attempts - 1, 0)), 900)
                job.next_attempt_at = now + timedelta(seconds=delay_seconds)
                job.finished_at = None
                job.progress_message = f"Recovered after worker lease expiry; retry in {delay_seconds}s"
            else:
                job.status = "failed"
                job.next_attempt_at = None
                job.finished_at = now
                job.progress_message = "Failed after worker lease expiry and maximum attempts"
            job.error = "Worker lease expired; job recovered by watchdog"
            job.lease_expires_at = None
            job.worker_id = None
            db.add(SystemEvent(level="WARNING", event_type="JOB_RECOVERED",
                               message=f"Recovered expired {job.job_type} job",
                               details={"job_id": job.id, "attempt": job.attempts,
                                        "status": job.status}))


def job_to_dict(job: Job) -> dict:
    return {
        "id": job.id, "type": job.job_type, "status": job.status,
        "progress": job.progress, "progress_message": job.progress_message,
        "result": job.result, "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "next_attempt_at": job.next_attempt_at.isoformat() if job.next_attempt_at else None,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "worker_id": job.worker_id,
        "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def renew_job_lease(job_id: str, *, worker_id: str | None = None) -> bool:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        if not job or job.status != "running":
            return False
        expected_owner = worker_id or _ACTIVE_WORKER_ID.get()
        if expected_owner and job.worker_id != expected_owner:
            return False
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().job_lease_seconds)
        return True
