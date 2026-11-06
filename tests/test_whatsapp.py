import hashlib
import hmac
import logging
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from rslr.whatsapp import verify_webhook_signature


def test_webhook_signature_rejects_missing_signature():
    assert verify_webhook_signature(b"{}", None) is False


def test_webhook_signature_matches_hmac(monkeypatch):
    import rslr.whatsapp as whatsapp
    from dataclasses import replace
    secret = "test-secret"
    monkeypatch.setattr(whatsapp, "settings", replace(whatsapp.settings, whatsapp_app_secret=secret))
    body = b'{"object":"whatsapp_business_account"}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, sig) is True
    assert verify_webhook_signature(body, "sha256=bad") is False


def test_send_text_logs_meta_error_code_on_400(monkeypatch, caplog):
    import rslr.whatsapp as whatsapp

    monkeypatch.setattr(
        whatsapp,
        "settings",
        replace(
            whatsapp.settings,
            whatsapp_access_token="test-token",
            whatsapp_phone_number_id="1285387147997440",
        ),
    )
    client = whatsapp.WhatsAppClient()

    def fake_post(url, headers=None, json=None):
        request = httpx.Request("POST", url)
        return httpx.Response(
            400,
            json={"error": {"code": 131030, "message": "Recipient phone number not in allowed list"}},
            request=request,
        )

    monkeypatch.setattr(client, "http", SimpleNamespace(post=fake_post))
    with caplog.at_level(logging.ERROR, logger="rslr.whatsapp"):
        with pytest.raises(httpx.HTTPStatusError):
            client.send_text("923001234567", "hi")
    assert "131030" in caplog.text
    assert "Recipient phone number not in allowed list" in caplog.text
