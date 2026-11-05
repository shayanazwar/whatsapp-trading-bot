from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.whatsapp import WhatsAppCloud


def signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_routes_research_backtest_command_for_allowlisted_sender(tmp_path, monkeypatch):
    base = load_settings()
    cfg = replace(
        base,
        database_path=tmp_path / "webhook.sqlite3",
        reports_dir=tmp_path / "reports",
        meta_verify_token="verify-me",
        meta_app_secret="test-app-secret",
        meta_phone_number_id="test-phone-id",
        whatsapp_allowed_senders=("923001234567",),
    )
    sent: list[tuple[str, str]] = []

    async def fake_send_text(self, recipient: str, text: str):
        sent.append((recipient, text))
        return {"messages": [{"id": "wamid.outbound.test"}]}

    monkeypatch.setattr(WhatsAppCloud, "send_text", fake_send_text)
    app = create_app(cfg)
    with TestClient(app) as client:
        for path in ("/webhook/whatsapp", "/webhook"):
            response = client.get(path, params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "challenge-123",
            })
            assert response.status_code == 200
            assert response.text == "challenge-123"

        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "test-phone-id"},
            "messages": [{"id": "wamid.in.backtest", "from": "923001234567", "timestamp": str(int(time.time())),
                          "type": "text", "text": {"body": "BACKTEST 1D"}}],
        }}]}]}
        body = json.dumps(payload, separators=(",", ":")).encode()
        response = client.post(
            "/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body, "test-app-secret")},
        )
        assert response.status_code == 200
        for _ in range(5):
            client.get("/healthz")
            if sent:
                break
        assert len(sent) == 1
        assert sent[0][0] == "923001234567"
        assert "BACKTEST JOB QUEUED" in sent[0][1]
        assert app.state.store.recent_events(10, "WHATSAPP_COMMAND_PROCESSED")
        assert app.state.store.recent_events(10, "BACKTEST_JOB_QUEUED")
        jobs = app.state.store.recent_backtest_jobs(5, recipient="923001234567")
        assert len(jobs) == 1 and jobs[0]["status"] == "queued"
        assert client.get("/healthz").json()["whatsapp_commands_enabled"] is True


@pytest.mark.asyncio
async def test_whatsapp_cloud_send_text_works_without_a_clearance_gate():
    cfg = replace(
        load_settings(),
        meta_access_token="test-access-token",
        meta_phone_number_id="test-phone-id",
        whatsapp_allowed_senders=("923001234567",),
    )
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.research-reply"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cloud = WhatsAppCloud(cfg, client=client)
    try:
        result = await cloud.send_text("+92 300 1234567", "BACKTEST JOB QUEUED")
    finally:
        await client.aclose()

    assert result["messages"][0]["id"] == "wamid.research-reply"
    assert str(captured["url"]).endswith("/v26.0/test-phone-id/messages")
    assert captured["authorization"] == "Bearer test-access-token"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["to"] == "923001234567"
    assert payload["text"]["body"] == "BACKTEST JOB QUEUED"


def test_webhook_rejects_invalid_signature(tmp_path):
    base = load_settings()
    cfg = replace(
        base, database_path=tmp_path / "signature.sqlite3",
        reports_dir=tmp_path / "reports", meta_verify_token="v", meta_app_secret="s",
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        for path in ("/webhook/whatsapp", "/webhook"):
            response = client.post(
                path, content=b'{"entry":[]}',
                headers={"X-Hub-Signature-256": "sha256=invalid"},
            )
            assert response.status_code == 403


def test_health_reports_scheduler_disabled_by_default(tmp_path):
    base = load_settings()
    cfg = replace(
        base, database_path=tmp_path / "health.sqlite3", reports_dir=tmp_path / "reports",
        scheduler_enabled=False, paper_trading_enabled=False,
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        responses = [client.get("/healthz"), client.get("/health"), client.head("/")]
    assert all(response.status_code == 200 for response in responses)
    body = responses[0].json()
    assert body["scheduler_enabled"] is False
    assert body["scheduler_worker"] == "disabled"
    assert body["live_execution_enabled"] is False


def test_status_only_webhook_is_acknowledged_without_creating_replies(tmp_path):
    base = load_settings()
    cfg = replace(
        base, database_path=tmp_path / "status-only.sqlite3", reports_dir=tmp_path / "reports",
        meta_app_secret="test-app-secret",
    )
    app = create_app(cfg)
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "statuses": [{"id": "wamid.sent", "status": "delivered", "recipient_id": "923001234567"}],
    }}]}]}
    body = json.dumps(payload, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = client.post(
            "/webhook", content=body,
            headers={"X-Hub-Signature-256": signature(body, "test-app-secret")},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}
        assert app.state.store.recent_events(10, "WHATSAPP_MESSAGE_ACCEPTED_BY_META") == []


def test_help_command_returns_full_supported_command_catalog_over_webhook(tmp_path, monkeypatch):
    from app.commands import HELP_TEXT

    base = load_settings()
    cfg = replace(
        base, database_path=tmp_path / "help.sqlite3", reports_dir=tmp_path / "reports",
        meta_app_secret="help-secret", meta_phone_number_id="test-phone-id",
        whatsapp_allowed_senders=("923001234567",),
    )
    sent: list[str] = []

    async def fake_send_text(self, recipient: str, text: str):
        sent.append(text)
        return {"messages": [{"id": "wamid.help"}]}

    monkeypatch.setattr(WhatsAppCloud, "send_text", fake_send_text)
    app = create_app(cfg)
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "test-phone-id"},
        "messages": [{"id": "wamid.in.help", "from": "923001234567", "timestamp": str(int(time.time())),
                      "type": "text", "text": {"body": "HELP"}}],
    }}]}]}
    body = json.dumps(payload, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = client.post("/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature(body, "help-secret")})
        assert response.status_code == 200
        for _ in range(5):
            client.get("/healthz")
            if sent:
                break
        assert sent == [HELP_TEXT]
        assert "BACKTEST REPORT <JOB_ID>" in sent[0]
        assert "ALERT BTCUSDT" in sent[0]
        assert app.state.store.recent_events(10, "WHATSAPP_COMMAND_PROCESSED")


def test_health_exposes_worker_heartbeat_freshness(tmp_path):
    from datetime import datetime, timezone

    base = load_settings()
    cfg = replace(
        base, database_path=tmp_path / "scanner-health.sqlite3",
        reports_dir=tmp_path / "reports", mexc_universe_scan_enabled=True,
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        body = client.get("/healthz").json()
        assert body["scanner_health"] == "scheduler_disabled"
        assert body["worker_health"] == "healthy"
        app.state.store.set_state("worker_status", "running")
        app.state.store.set_state("worker_heartbeat_utc", datetime.now(timezone.utc).isoformat())
        body = client.get("/healthz").json()
        assert body["scanner_health"] == "scheduler_disabled"
        assert body["worker_health"] == "healthy"
        assert body["scanner_worker_heartbeat_age_seconds"] is not None


@pytest.mark.asyncio
async def test_delayed_backtest_webhook_command_is_queued_once(tmp_path):
    from app.services.command_processor import CommandProcessor
    from app.storage import Store

    base = load_settings()
    cfg = replace(
        base,
        database_path=tmp_path / "delayed-backtest.sqlite3",
        reports_dir=tmp_path / "reports",
        whatsapp_allowed_senders=("923001234567",),
    )
    store = Store(cfg.database_path)
    processor = CommandProcessor(cfg, store, mexc=object(), whatsapp=object())
    delayed_timestamp = int(time.time()) - 3600

    first = await processor.process_text(
        "923001234567", "BACKTEST 1D", received_timestamp=delayed_timestamp,
        request_id="wamid.delayed.backtest", enforce_sender_allowlist=True,
    )
    second = await processor.process_text(
        "923001234567", "BACKTEST 1D", received_timestamp=delayed_timestamp,
        request_id="wamid.delayed.backtest", enforce_sender_allowlist=True,
    )

    assert "BACKTEST JOB QUEUED" in first.text
    assert "BACKTEST JOB QUEUED" in second.text
    assert first.text.split("Job: ", 1)[1].splitlines()[0] == second.text.split("Job: ", 1)[1].splitlines()[0]
    jobs = store.recent_backtest_jobs(10, recipient="923001234567")
    assert len(jobs) == 1
    assert jobs[0]["args"]["strategy_scope"] == "s1-rfpc"
