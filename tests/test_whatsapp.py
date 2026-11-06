import hashlib
import hmac

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
