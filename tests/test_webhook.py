from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import settings as default_settings
from app.whatsapp import WhatsAppClient


class DummyMexc:
    async def aclose(self):
        return None


class RecordingWhatsApp(WhatsAppClient):
    sent = []

    async def send_chunks(self, recipient: str, text: str, max_chars: int = 3500):
        self.sent.append((recipient, text))


def test_webhook_authentication_and_sender_allowlist(tmp_path, monkeypatch):
    secret = "test-app-secret"
    cfg = replace(
        default_settings,
        database_path=str(tmp_path / "webhook.sqlite3"),
        monitor_enabled=False,
        wa_phone_number_id="phone-id",
        wa_access_token="test-token",
        wa_verify_token="verify-me",
        wa_app_secret=secret,
        authorized_wa_ids=("111",),
        wa_recipients=(),
    )
    monkeypatch.setattr(main_module, "settings", cfg)
    monkeypatch.setattr(main_module, "MexcClient", lambda _settings: DummyMexc())
    RecordingWhatsApp.sent = []
    monkeypatch.setattr(main_module, "WhatsAppClient", lambda _settings: RecordingWhatsApp(cfg))

    with TestClient(main_module.app) as client:
        verified = client.get("/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "abc123",
        })
        assert verified.status_code == 200 and verified.text == "abc123"

        unauthorized_body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": "999", "id": "wamid-unauthorized", "text": {"body": "/help"}}
        ]}}]}]}).encode()
        sig = "sha256=" + hmac.new(secret.encode(), unauthorized_body, hashlib.sha256).hexdigest()
        response = client.post("/webhook", content=unauthorized_body, headers={"X-Hub-Signature-256": sig})
        assert response.status_code == 200
        assert RecordingWhatsApp.sent == []

        authorized_body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": "111", "id": "wamid-authorized", "text": {"body": "/help"}}
        ]}}]}]}).encode()
        sig = "sha256=" + hmac.new(secret.encode(), authorized_body, hashlib.sha256).hexdigest()
        response = client.post("/webhook", content=authorized_body, headers={"X-Hub-Signature-256": sig})
        assert response.status_code == 200
        assert len(RecordingWhatsApp.sent) == 1
        assert "BTCUSDT MEXC SIGNAL BOT" in RecordingWhatsApp.sent[0][1]

        invalid = client.post("/webhook", content=b'{"entry":[]}', headers={"X-Hub-Signature-256": "sha256=bad"})
        assert invalid.status_code == 403
