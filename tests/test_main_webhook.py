from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app


def signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_verify_and_crypto_policy_gate_blocks_commands(tmp_path):
    base = load_settings()
    cfg = replace(
        base,
        database_path=tmp_path / "webhook.sqlite3",
        reports_dir=tmp_path / "reports",
        meta_verify_token="verify-me",
        meta_app_secret="test-app-secret",
        whatsapp_allowed_senders=("923001234567",),
        whatsapp_crypto_messaging_allowed=False,
        whatsapp_policy_clearance_ref="",
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        response = client.get("/webhook/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "challenge-123",
        })
        assert response.status_code == 200
        assert response.text == "challenge-123"

        payload = {"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "not-used"},
            "messages": [{"id": "wamid.in.1", "from": "923001234567", "timestamp": "1793000000",
                          "type": "text", "text": {"body": "STATUS"}}],
        }}]}]}
        body = json.dumps(payload, separators=(",", ":")).encode()
        response = client.post("/webhook/whatsapp", content=body,
                               headers={"X-Hub-Signature-256": signature(body, "test-app-secret")})
        assert response.status_code == 200
        assert app.state.store.recent_events(10, "WHATSAPP_POLICY_GATE_BLOCKED")
        assert app.state.store.pending_outbox() == []


def test_webhook_rejects_invalid_signature(tmp_path):
    base = load_settings()
    cfg = replace(base, database_path=tmp_path / "webhook.sqlite3", reports_dir=tmp_path / "reports",
                  meta_verify_token="v", meta_app_secret="s")
    app = create_app(cfg)
    with TestClient(app) as client:
        response = client.post("/webhook/whatsapp", content=b'{"entry":[]}',
                               headers={"X-Hub-Signature-256": "sha256=invalid"})
        assert response.status_code == 403


def test_health_reports_scheduler_disabled_by_default(tmp_path):
    base = load_settings()
    cfg = replace(base, database_path=tmp_path / "health.sqlite3", reports_dir=tmp_path / "reports",
                  scheduler_enabled=False, paper_trading_enabled=False)
    app = create_app(cfg)
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["scheduler_enabled"] is False
    assert body["scheduler_worker"] == "disabled"
    assert body["live_execution_enabled"] is False
