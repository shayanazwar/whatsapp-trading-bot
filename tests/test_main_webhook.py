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
from app.commands import WHATSAPP_SUPPORT_HELP_TEXT
from app.main import create_app
from app.whatsapp import WHATSAPP_POLICY_BLOCKED_REPLY, WhatsAppCloud, WhatsAppPolicyBlocked


def signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_closed_policy_gate_blocks_crypto_command_with_clear_reply(tmp_path, monkeypatch):
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

    monkeypatch.setattr(
        WhatsAppCloud, "send_service_status", fake_service_status, raising=False
    )

    app = create_app(cfg)
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "test-phone-id"},
        "messages": [{"id": "wamid.in.price", "from": "923001234567", "timestamp": str(int(time.time())),
                      "type": "text", "text": {"body": "PRICE BTCUSDT"}}],
    }}]}]}
    body = json.dumps(payload, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = client.post(
            "/webhook/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": signature(body, "test-app-secret")},
        )
        assert response.status_code == 200
        client.get("/healthz")  # yield to the background reply task
        assert sent_to == ["923001234567"]
        assert app.state.store.recent_events(10, "WHATSAPP_POLICY_GATE_BLOCKED")
        assert app.state.store.recent_events(10, "WHATSAPP_SERVICE_STATUS_ACCEPTED_BY_META")
        assert not app.state.store.recent_events(10, "WHATSAPP_COMMAND_PROCESSED")
        assert app.state.store.pending_outbox() == []
        with app.state.store.connection() as conn:
            outbox = conn.execute(
                "SELECT body FROM outbox WHERE idempotency_key=?",
                ("wa-policy-status:wamid.in.price",),
            ).fetchone()
        assert outbox is not None
        assert outbox["body"] == WHATSAPP_POLICY_BLOCKED_REPLY
        assert "HELP and STATUS only" in outbox["body"]
        assert "not executed" in outbox["body"].lower()
        assert "Automated commands are not enabled" not in outbox["body"]


def test_closed_gate_processes_help_and_status_as_support_commands(tmp_path, monkeypatch):
    base = load_settings()
    cfg = replace(
        base,
        database_path=tmp_path / "support.sqlite3",
        reports_dir=tmp_path / "reports",
        meta_app_secret="test-app-secret",
        meta_access_token="test-token",
        meta_phone_number_id="test-phone-id",
        whatsapp_allowed_senders=("923001234567",),
        whatsapp_crypto_messaging_allowed=False,
        whatsapp_policy_clearance_ref="",
    )
    sent: list[tuple[str, str, str]] = []

    async def fake_support_text(self, recipient: str, text: str, *, command: str):
        sent.append((recipient, command, text))
        return {"messages": [{"id": f"wamid.support.{command.lower()}"}]}

    monkeypatch.setattr(WhatsAppCloud, "send_support_text", fake_support_text, raising=False)
    app = create_app(cfg)
    current = str(int(time.time()))
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "test-phone-id"},
        "messages": [
            {"id": "wamid.in.help", "from": "923001234567", "timestamp": current,
             "type": "text", "text": {"body": "HELP"}},
            {"id": "wamid.in.status", "from": "923001234567", "timestamp": current,
             "type": "text", "text": {"body": "STATUS"}},
        ],
    }}]}]}
    body = json.dumps(payload, separators=(",", ":")).encode()
    with TestClient(app) as client:
        response = client.post(
            "/webhook/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": signature(body, "test-app-secret")},
        )
        assert response.status_code == 200
        # Let the background tasks complete.
        for _ in range(3):
            client.get("/healthz")
            if len(sent) == 2:
                break

        assert {entry[1] for entry in sent} == {"HELP", "STATUS"}
        help_text = next(text for _, command, text in sent if command == "HELP")
        status_text = next(text for _, command, text in sent if command == "STATUS")
        assert "🤖 TRADING AI — COMMAND MENU" in help_text
        assert "HELP — Show this menu" in help_text
        assert "PRICE BTCUSDT — Current price" in help_text
        assert "BACKTEST 7D — 7-day backtest" in help_text
        assert "https://shayan-millionaire-bot.onrender.com/dashboard" in help_text
        assert "Crypto prices, analysis, scans, signals and backtests are not available" not in help_text
        assert "SYSTEM STATUS" in status_text
        assert len(app.state.store.recent_events(10, "WHATSAPP_COMMAND_PROCESSED")) == 2
        assert not app.state.store.recent_events(10, "WHATSAPP_POLICY_GATE_BLOCKED")
        assert app.state.store.pending_outbox() == []


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



@pytest.mark.asyncio
async def test_adapter_sends_only_help_status_when_gate_closed():
    cfg = replace(
        load_settings(),
        meta_access_token="test-access-token",
        meta_phone_number_id="test-phone-id",
        whatsapp_allowed_senders=("923001234567",),
        whatsapp_crypto_messaging_allowed=False,
        whatsapp_policy_clearance_ref="",
    )
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.support"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cloud = WhatsAppCloud(cfg, client=client)
    try:
        await cloud.send_support_text(
            "923001234567", WHATSAPP_SUPPORT_HELP_TEXT, command="HELP"
        )
        with pytest.raises(WhatsAppPolicyBlocked):
            await cloud.send_support_text(
                "923001234567", "A market price", command="PRICE"
            )
    finally:
        await client.aclose()

    assert len(captured) == 1
    assert captured[0]["text"]["body"] == WHATSAPP_SUPPORT_HELP_TEXT



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
