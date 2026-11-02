from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.analysis import analyze_series, format_analysis
from app.commands import Command
from app.config import load_settings
from app.services.scheduler import ResearchScheduler
from app.storage import Store


class DummyWhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient: str, body: str):
        self.sent.append((recipient, body))
        return {"messages": [{"id": "wamid.test-running"}]}

    async def aclose(self):
        return None


class DummyBacktestProcessor:
    async def execute(self, command: Command):
        assert command.name == "BACKTEST"
        assert command.args["_job_id"]
        return SimpleNamespace(text="BACKTEST REPORT: completed", event_type="BACKTEST_COMPLETED")


def test_analyze_explicitly_discloses_missing_token_fundamentals():
    report = analyze_series("BTC_USDT", {"1D": [], "12H": [], "4H": [], "1H": []})
    assert report["fundamentals"]["status"] == "NOT_IMPLEMENTED"
    assert report["fundamentals"]["provider"] is None
    assert report["fundamentals"]["data"] == {}
    formatted = format_analysis(report)
    assert "Fundamentals: NOT_IMPLEMENTED" in formatted
    assert "Funding rates are derivatives carry data" in formatted


@pytest.mark.asyncio
async def test_worker_claims_job_logs_running_status_and_persists_result(tmp_path, caplog):
    base = load_settings()
    settings = replace(
        base,
        database_path=tmp_path / "worker-jobs.sqlite3",
        reports_dir=tmp_path / "reports",
        whatsapp_crypto_messaging_allowed=False,
        whatsapp_policy_clearance_ref="",
    )
    store = Store(settings.database_path)
    whatsapp = DummyWhatsApp()
    scheduler = ResearchScheduler(
        settings, store, mexc=object(), whatsapp=whatsapp,
        backtest_processor=DummyBacktestProcessor(),
    )
    job_id = store.enqueue_backtest_job(
        "923001234567", {"window": "1D"}, idempotency_key="release-v081-worker-job",
    )

    with caplog.at_level(logging.INFO):
        await scheduler._process_next_backtest_job()

    job = store.get_backtest_job(job_id, recipient="923001234567")
    assert job is not None
    assert job["status"] == "complete"
    assert job["result"]["text"] == "BACKTEST REPORT: completed"
    assert len(whatsapp.sent) == 1
    assert "BACKTEST JOB RUNNING" in whatsapp.sent[0][1]
    assert "BACKTEST JOB START" in caplog.text
    assert "BACKTEST JOB COMPLETE" in caplog.text
    # Crypto research report delivery remains blocked while its distinct policy gate is closed.
    assert not any("BACKTEST REPORT: completed" in body for _, body in whatsapp.sent)


@pytest.mark.asyncio
async def test_worker_health_is_visible_when_optional_scanner_is_disabled(tmp_path):
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from app.main import create_app

    base = load_settings()
    settings = replace(
        base,
        database_path=tmp_path / "worker-health.sqlite3",
        reports_dir=tmp_path / "reports",
        mexc_universe_scan_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        store = app.state.store
        store.set_state("worker_status", "running")
        store.set_state("worker_heartbeat_utc", datetime.now(timezone.utc).isoformat())
        store.set_state("worker_database_fingerprint", store.database_fingerprint)
        store.set_state("worker_build_version", "0.8.1")
        body = client.get("/healthz").json()

    assert body["scanner_enabled"] is False
    assert body["scanner_health"] == "disabled"
    assert body["worker_health"] == "healthy"
    assert body["worker_status"] == "running"
    assert body["worker_database_match"] is True
    assert body["worker_build_version"] == "0.8.1"


def test_backtest_poll_setting_is_validated(monkeypatch):
    monkeypatch.setenv("BACKTEST_JOB_POLL_SECONDS", "7")
    assert load_settings().backtest_job_poll_seconds == 7
    monkeypatch.setenv("BACKTEST_JOB_POLL_SECONDS", "0")
    with pytest.raises(ValueError, match="BACKTEST_JOB_POLL_SECONDS"):
        load_settings()


@pytest.mark.asyncio
async def test_backtest_start_log_precedes_universe_request(tmp_path, caplog):
    from app.services.command_processor import CommandProcessor

    class DummyMexc:
        async def server_time(self):
            return 1_800_000_000

    settings = replace(
        load_settings(),
        database_path=tmp_path / "early-backtest-log.sqlite3",
        reports_dir=tmp_path / "reports",
    )
    processor = CommandProcessor(settings, Store(settings.database_path), mexc=DummyMexc())

    async def fail_universe(_limit: int):
        raise RuntimeError("fixture: universe fetch unavailable")

    processor._load_top_universe = fail_universe
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="universe fetch"):
            await processor._run_backtest({"window": "1D", "windows": ["1D"]})

    start_pos = caplog.text.index("BACKTEST START")
    universe_pos = caplog.text.index("BACKTEST UNIVERSE LOAD START")
    assert start_pos < universe_pos


@pytest.mark.asyncio
async def test_backtest_queue_is_not_blocked_by_slow_market_cycle(tmp_path):
    """A slow scanner/API cycle must not delay a pending durable backtest job."""
    base = load_settings()
    settings = replace(
        base,
        database_path=tmp_path / "independent-queue.sqlite3",
        reports_dir=tmp_path / "reports",
        backtest_job_poll_seconds=1,
        scheduler_interval_seconds=60,
        whatsapp_crypto_messaging_allowed=False,
        whatsapp_policy_clearance_ref="",
    )
    store = Store(settings.database_path)
    whatsapp = DummyWhatsApp()
    started = asyncio.Event()

    class WaitingBacktestProcessor:
        async def execute(self, command: Command):
            assert command.name == "BACKTEST"
            started.set()
            return SimpleNamespace(text="BACKTEST REPORT: completed", event_type="BACKTEST_COMPLETED")

    scheduler = ResearchScheduler(
        settings, store, mexc=object(), whatsapp=whatsapp,
        backtest_processor=WaitingBacktestProcessor(),
    )
    job_id = store.enqueue_backtest_job(
        "923001234567", {"window": "1D"}, idempotency_key="release-v081-independent-queue",
    )
    market_cycle_entered = asyncio.Event()
    release_market_cycle = asyncio.Event()

    async def slow_market_cycle(*, process_backtest=True):
        assert process_backtest is False
        market_cycle_entered.set()
        await release_market_cycle.wait()

    scheduler.run_once = slow_market_cycle
    worker_task = asyncio.create_task(scheduler.run_forever())
    try:
        await asyncio.wait_for(market_cycle_entered.wait(), timeout=1.0)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert store.get_backtest_job(job_id, recipient="923001234567")["status"] == "running"
    finally:
        scheduler.stop()
        release_market_cycle.set()
        await asyncio.wait_for(worker_task, timeout=2.0)

    assert store.get_backtest_job(job_id, recipient="923001234567")["status"] == "complete"


def test_shared_price_tick_rounding_is_conservative_and_independent_of_legacy_backtester():
    from app.market.precision import round_trigger_to_tick
    from app.backtest_rsi_ema import _round_trigger_to_tick

    assert round_trigger_to_tick(100.01, 0.1, "LONG", "STOP") == 100.1
    assert round_trigger_to_tick(100.01, 0.1, "LONG", "TARGET") == 100.0
    assert round_trigger_to_tick(100.01, 0.1, "SHORT", "STOP") == 100.0
    assert round_trigger_to_tick(100.01, 0.1, "SHORT", "TARGET") == 100.1
    assert round_trigger_to_tick(100.01, 0.0, "LONG", "STOP") == 100.01
    # Historical private import remains compatible while the live scanner uses app.market.
    assert _round_trigger_to_tick(100.01, 0.1, "LONG", "STOP") == 100.1


def test_render_blueprint_has_matching_web_and_worker_database_and_queue_settings():
    import yaml
    from pathlib import Path

    blueprint = yaml.safe_load(Path("render.yaml").read_text(encoding="utf-8"))
    services = {row["name"]: row for row in blueprint["services"]}
    web = services["mexc-rsi-ema-web"]
    worker = services["mexc-rsi-ema-scanner"]
    assert web["type"] == "web"
    assert worker["type"] == "worker"
    assert worker["dockerCommand"] == "python -m app.worker"

    def env_map(service):
        return {row["key"]: row for row in service["envVars"]}

    web_env = env_map(web)
    worker_env = env_map(worker)
    assert web_env["DATABASE_URL"]["fromDatabase"] == worker_env["DATABASE_URL"]["fromDatabase"]
    assert web_env["DATABASE_URL"]["fromDatabase"]["name"] == "mexc-rsi-ema-db"
    assert web_env["BACKTEST_JOB_POLL_SECONDS"]["value"] == worker_env["BACKTEST_JOB_POLL_SECONDS"]["value"] == "5"
    # No legacy Render plan IDs are forced onto existing resources.
    assert "plan" not in web and "plan" not in worker and "plan" not in blueprint["databases"][0]


def test_active_strategy_timeframes_and_env_example_are_aligned():
    from pathlib import Path
    from app.market.candles import STRATEGY_TIMEFRAMES

    assert STRATEGY_TIMEFRAMES == {"1D", "12H", "4H", "1H"}
    env_example = Path(".env.example").read_text(encoding="utf-8")
    assert "STRATEGY_VERSION=S1-RFPC" in env_example
    assert "STRATEGY_TIMEFRAME=3M" not in env_example
    assert "BACKTEST_MAX_POSITIONS=50" in env_example
    assert "LIVE_EXECUTION_ENABLED=false" in env_example


@pytest.mark.asyncio
async def test_general_outbox_does_not_double_send_immediate_backtest_status(tmp_path):
    base = load_settings()
    settings = replace(
        base,
        database_path=tmp_path / "backtest-status-outbox.sqlite3",
        reports_dir=tmp_path / "reports",
        whatsapp_crypto_messaging_allowed=True,
        whatsapp_policy_clearance_ref="operator-recorded-test-clearance",
        whatsapp_monthly_message_cap=50,
    )
    store = Store(settings.database_path)
    whatsapp = DummyWhatsApp()
    scheduler = ResearchScheduler(settings, store, mexc=object(), whatsapp=whatsapp)
    store.enqueue_outbox(
        "923001234567", "BACKTEST JOB RUNNING", category="backtest_status",
        idempotency_key="backtest-status-outbox-dedup",
    )
    store.enqueue_outbox(
        "923001234567", "Approved research report", category="crypto_research",
        idempotency_key="crypto-report-outbox-flush",
    )

    await scheduler._flush_outbox()

    assert [body for _, body in whatsapp.sent] == ["Approved research report"]

