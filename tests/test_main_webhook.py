from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.whatsapp import WHATSAPP_POLICY_BLOCKED_REPLY, WhatsAppCloud


def signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_closed_policy_gate_sends_only_generic_service_reply(tmp_path, monkeypatch):
    base = load_settings()
    cfg = replace(
        base,
        database_path=tmp_path / "webhook.sqlite3",
        reports_dir=tmp_path / "reports",
        meta_verify_token="verify-me",
        meta_app_secret="test-app-secret",
        meta_access_token="test-token",
        meta_phone_number_id="test-phone-id",
        whatsapp_allowed_senders=("923001234567",),
        whatsapp_crypto_messaging_allowed=False,
        whatsapp_policy_clearance_ref="",
    )
    sent_to: list[str] = []

    async def fake_service_status(self, recipient: str):
        sent_to.append(recipient)
        return {"messages": [{"id": "wamid.outbound.test"}]}

    # The current main branch provides this method. raising=False also lets this
    # test run against the earlier source ZIP before that helper was added.
    monkeypatch.setattr(
        WhatsAppCloud, "send_service_status", fake_service_status, raising=False
    )

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
            "messages": [{"id": "wamid.in.1", "from": "923001234567", "timestamp": "1793000000",
                          "type": "text", "text": {"body": "STATUS"}}],
        }}]}]}
        body = json.dumps(payload, separators=(",", ":")).encode()
        for path in ("/webhook/whatsapp",):
            response = client.post(
                path, content=body,
                headers={"X-Hub-Signature-256": signature(body, "test-app-secret")},
            )
            assert response.status_code == 200
        # A follow-up request yields the event loop to the background responder.
        assert client.get("/healthz").status_code == 200
        assert sent_to == ["923001234567"]
        assert app.state.store.recent_events(10, "WHATSAPP_POLICY_GATE_BLOCKED")
        assert app.state.store.recent_events(10, "WHATSAPP_SERVICE_STATUS_ACCEPTED_BY_META")
        assert not app.state.store.recent_events(10, "WHATSAPP_COMMAND_PROCESSED")
        assert app.state.store.pending_outbox() == []
        # The durable outbox must record the exact text submitted to Meta, not a stale fallback.
        with app.state.store.connection() as conn:
            outbox = conn.execute(
                "SELECT body FROM outbox WHERE idempotency_key=?",
                ("wa-policy-status:wamid.in.1",),
            ).fetchone()
        assert outbox is not None
        assert outbox["body"] == WHATSAPP_POLICY_BLOCKED_REPLY
        assert "no command was executed" in outbox["body"].lower()
        assert "onrender.com/dashboard" not in outbox["body"]


@pytest.mark.asyncio
async def test_policy_status_sender_uses_canonical_message_and_allowlist():
    cfg = replace(
        load_settings(),
        meta_access_token="test-access-token",
        meta_phone_number_id="test-phone-id",
        whatsapp_allowed_senders=("923001234567",),
        whatsapp_crypto_messaging_allowed=False,
        whatsapp_policy_clearance_ref="",
    )
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.policy-reply"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cloud = WhatsAppCloud(cfg, client=client)
    try:
        result = await cloud.send_service_status("+92 300 1234567")
    finally:
        await client.aclose()

    assert result["messages"][0]["id"] == "wamid.policy-reply"
    assert captured["url"].endswith("/v26.0/test-phone-id/messages")
    assert captured["authorization"] == "Bearer test-access-token"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["to"] == "923001234567"
    assert payload["text"]["body"] == WHATSAPP_POLICY_BLOCKED_REPLY
    assert "onrender.com/dashboard" not in payload["text"]["body"]


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
