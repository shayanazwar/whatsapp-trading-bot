from app.services.jobs import enqueue_job, claim_job, job_to_dict


def test_enqueue_is_idempotent_for_active_payload():
    a = enqueue_job("analysis", {"symbol":"BTC_USDT"})
    b = enqueue_job("analysis", {"symbol":"BTC_USDT"})
    assert a.id == b.id


def test_claim_job_by_role():
    queued = enqueue_job("forecast", {"symbol":"BTC_USDT"})
    assert claim_job("market") is None
    job = claim_job("compute")
    assert job.id == queued.id
    assert job.status == "running"


def test_job_to_dict_has_progress_fields():
    job = enqueue_job("scan", {})
    result = job_to_dict(job)
    assert result["status"] == "queued"
    assert result["progress"] == 0
