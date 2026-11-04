from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import Job
from app.services.jobs import enqueue_job, claim_job, fail_job, finish_job, job_to_dict


def test_enqueue_is_idempotent_for_active_payload():
    a = enqueue_job("analysis", {"symbol":"BTC_USDT"})
    b = enqueue_job("analysis", {"symbol":"BTC_USDT"})
    assert a.id == b.id


def test_claim_job_by_role():
    queued = enqueue_job("forecast", {"symbol":"BTC_USDT"})
    assert claim_job("market") is None
    # Forecasting/analysis jobs belong to the inference worker, not the
    # backtest compute worker. This prevents long research jobs starving signals.
    assert claim_job("compute") is None
    job = claim_job("inference")
    assert job.id == queued.id
    assert job.status == "running"


def test_job_to_dict_has_progress_fields():
    job = enqueue_job("scan", {})
    result = job_to_dict(job)
    assert result["status"] == "queued"
    assert result["progress"] == 0


def test_failed_job_uses_persisted_bounded_retry_schedule():
    job = enqueue_job("analysis", {"symbol": "ETH_USDT"}, max_attempts=2)
    claimed = claim_job("inference", worker_id="inference-test")
    assert claimed.id == job.id
    fail_job(job.id, "temporary model download failure")
    with SessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.status == "queued"
        assert row.next_attempt_at is not None
        retry_at = row.next_attempt_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        assert retry_at > datetime.now(timezone.utc)
        assert row.progress_message.startswith("Retry scheduled")
    # It must not be reclaimed before its persisted retry time.
    assert claim_job("inference", worker_id="inference-test-2") is None


def test_job_retries_then_reaches_terminal_failed_state():
    job = enqueue_job("backtest", {"symbol": "BTC_USDT"}, max_attempts=1)
    claimed = claim_job("compute", worker_id="compute-test")
    assert claimed.id == job.id
    fail_job(job.id, "permanent invalid data")
    with SessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.status == "failed"
        assert row.finished_at is not None
        assert row.next_attempt_at is None


def test_finish_job_persists_result_and_clears_lease():
    job = enqueue_job("forecast", {"symbol": "BTC_USDT"})
    claimed = claim_job("inference", worker_id="inference-test")
    assert claimed.id == job.id
    finish_job(job.id, {"forecast_id": "forecast-1"})
    with SessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.status == "completed"
        assert row.progress == 100
        assert row.result == {"forecast_id": "forecast-1"}
        assert row.lease_expires_at is None


def test_stale_worker_cannot_overwrite_reclaimed_job():
    from app.services.jobs import worker_job_context

    queued = enqueue_job("analysis", {"symbol": "BTC_USDT"})
    claimed = claim_job("inference", worker_id="worker-a")
    assert claimed.id == queued.id
    # Simulate expiry/recovery and a different worker reclaiming the same job.
    with SessionLocal.begin() as db:
        row = db.get(Job, queued.id)
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    from app.services.jobs import recover_expired_jobs
    recover_expired_jobs()
    with SessionLocal.begin() as db:
        row = db.get(Job, queued.id)
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    claimed_again = claim_job("inference", worker_id="worker-b")
    assert claimed_again.id == queued.id

    with worker_job_context("worker-a"):
        assert finish_job(queued.id, {"stale": True}) is False
    with SessionLocal() as db:
        row = db.get(Job, queued.id)
        assert row.status == "running"
        assert row.worker_id == "worker-b"

    with worker_job_context("worker-b"):
        assert finish_job(queued.id, {"correct": True}) is True


def test_worker_and_pipeline_imports_resolve_restored_artifact_package():
    import app.worker  # noqa: F401
    from app.artifacts.store import DatabaseForecastArtifactStore
    from app.services.pipeline import process_job

    assert DatabaseForecastArtifactStore is not None
    assert callable(process_job)


def test_worker_consumes_and_finishes_job_end_to_end(monkeypatch):
    import signal
    from types import SimpleNamespace

    from app.services import pipeline
    from app import worker

    queued = enqueue_job("analysis", {"symbol": "BTC_USDT", "request_id": "req-worker-test"})
    real_claim = worker.claim_job
    claims = {"count": 0}

    def claim_once_then_stop(role, *, worker_id=None):
        claims["count"] += 1
        if claims["count"] == 1:
            return real_claim(role, worker_id=worker_id)
        worker.STOP = True
        return None

    monkeypatch.setattr(worker, "claim_job", claim_once_then_stop)
    monkeypatch.setattr(pipeline, "process_job", lambda job: {"status": "test-result"})
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
        worker_role="inference", worker_poll_seconds=0, job_lease_seconds=30,
        auto_scan_enabled=False, database_url="sqlite:///test.db", mexc_ws_url="",
        market_scan_interval_seconds=3600,
    ))
    monkeypatch.setattr(worker.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "notify_job_completion", lambda *_args, **_kwargs: None)
    worker.STOP = False
    try:
        worker.run("inference")
    finally:
        worker.STOP = False

    with SessionLocal() as db:
        completed = db.get(Job, queued.id)
        assert completed.status == "completed"
        assert completed.result == {"status": "test-result"}
        assert completed.progress == 100
