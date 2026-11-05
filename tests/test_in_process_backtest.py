from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.services.scheduler import ResearchScheduler
from app.storage import Store


@pytest.mark.asyncio
async def test_same_process_queue_consumer_claims_and_completes_job(tmp_path):
    settings = replace(
        load_settings(),
        database_url="",
        database_path=tmp_path / "in-process.sqlite3",
        reports_dir=tmp_path / "reports",
        market_data_dir=tmp_path / "market",
        whatsapp_allowed_senders=(),
        backtest_job_poll_seconds=1,
        mexc_universe_scan_enabled=False,
        paper_trading_enabled=False,
    )
    store = Store(settings.database_path)
    started = asyncio.Event()

    class FakeBacktestProcessor:
        async def execute(self, command):
            assert command.name == "BACKTEST"
            assert command.args["_job_id"]
            started.set()
            return SimpleNamespace(text="BACKTEST REPORT: test complete", event_type="BACKTEST_COMPLETED")

    scheduler = ResearchScheduler(
        settings, store, mexc=object(), whatsapp=None,
        backtest_processor=FakeBacktestProcessor(),
    )
    job_id = store.enqueue_backtest_job(
        "", {"window": "1D"}, idempotency_key="in-process-queue-test",
    )
    queue_task = asyncio.create_task(scheduler.run_backtest_queue_forever())
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        for _ in range(100):
            job = store.get_backtest_job(job_id)
            if job and job["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        assert store.get_backtest_job(job_id)["status"] == "complete"
        assert store.get_backtest_job(job_id)["result"]["text"] == "BACKTEST REPORT: test complete"
        assert store.get_state("worker_heartbeat_utc")
    finally:
        scheduler.stop()
        await asyncio.wait_for(queue_task, timeout=2.0)
    assert store.get_state("worker_status") == "stopped"
    assert store.get_state("backtest_executor_status") == "stopped"


def test_web_app_starts_backtest_consumer_without_market_scheduler(tmp_path):
    settings = replace(
        load_settings(),
        database_url="",
        database_path=tmp_path / "web-queue.sqlite3",
        reports_dir=tmp_path / "reports",
        market_data_dir=tmp_path / "market",
        scheduler_enabled=False,
        whatsapp_allowed_senders=(),
        backtest_job_poll_seconds=1,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert app.state.backtest_processor.backtest_inline is True
        assert app.state.scheduler.backtest_processor is app.state.backtest_processor
        assert health["in_process_backtest_queue"] is True
        assert health["backtest_executor_status"] == "running"
        assert health["scheduler_enabled"] is False


def test_store_requeues_active_job_when_web_process_is_shutting_down(tmp_path):
    store = Store(tmp_path / "requeue.sqlite3")
    job_id = store.enqueue_backtest_job(
        "", {"window": "1D"}, idempotency_key="requeue-after-shutdown",
    )
    assert store.claim_next_backtest_job("in-process-web")
    assert store.requeue_backtest_job(job_id, reason="test_shutdown") is True
    job = store.get_backtest_job(job_id)
    assert job["status"] == "queued"
    assert job["worker_id"] is None
    assert job["error"] is None
    assert job["progress"]["stage"] == "requeued_after_shutdown"


@pytest.mark.asyncio
async def test_cancelled_backtest_is_returned_to_queue(tmp_path):
    settings = replace(
        load_settings(), database_url="", database_path=tmp_path / "cancel.sqlite3",
        reports_dir=tmp_path / "reports", whatsapp_allowed_senders=(),
        backtest_job_poll_seconds=1, mexc_universe_scan_enabled=False,
        paper_trading_enabled=False,
    )
    store = Store(settings.database_path)
    started = asyncio.Event()

    class BlockingProcessor:
        async def execute(self, command):
            started.set()
            await asyncio.Event().wait()

    scheduler = ResearchScheduler(
        settings, store, mexc=object(), whatsapp=None,
        backtest_processor=BlockingProcessor(),
    )
    job_id = store.enqueue_backtest_job(
        "", {"window": "1D"}, idempotency_key="cancel-requeues-job",
    )
    task = asyncio.create_task(scheduler._process_next_backtest_job())
    await asyncio.wait_for(started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    job = store.get_backtest_job(job_id)
    assert job["status"] == "queued"
    assert job["progress"]["stage"] == "requeued_after_shutdown"


def test_dashboard_backtest_command_executes_through_same_web_process(tmp_path, monkeypatch):
    from app import main as main_module
    from app.services.command_processor import CommandProcessor as RealCommandProcessor, CommandResult

    execution_count = {"value": 0}

    class TestCommandProcessor(RealCommandProcessor):
        async def execute(self, command):
            if command.name == "BACKTEST" and self.backtest_inline:
                execution_count["value"] += 1
                return CommandResult(
                    "BACKTEST REPORT: in-process integration passed",
                    event_type="BACKTEST_COMPLETED",
                )
            return await super().execute(command)

    monkeypatch.setattr(main_module, "CommandProcessor", TestCommandProcessor)
    settings = replace(
        load_settings(),
        database_url="",
        database_path=tmp_path / "end-to-end.sqlite3",
        reports_dir=tmp_path / "reports",
        market_data_dir=tmp_path / "market",
        scheduler_enabled=False,
        whatsapp_allowed_senders=(),
        backtest_job_poll_seconds=1,
        web_api_token="test-dashboard-token",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/command",
            json={"command": "BACKTEST 1D"},
            headers={"Authorization": "Bearer test-dashboard-token"},
        )
        assert response.status_code == 200
        queued_text = response.json()["result"]
        assert "BACKTEST JOB QUEUED" in queued_text
        job_id = queued_text.split("Job: ", 1)[1].splitlines()[0]

        for _ in range(100):
            job = app.state.store.get_backtest_job(job_id)
            if job and job["status"] == "complete":
                break
            import time
            time.sleep(0.02)

        job = app.state.store.get_backtest_job(job_id)
        assert job["status"] == "complete"
        assert job["result"]["text"] == "BACKTEST REPORT: in-process integration passed"
        assert execution_count["value"] == 1
