import hashlib
import hmac
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from rslr import web, whatsapp
from rslr.storage import Database
from rslr.web import app

SECRET = "test-secret"
VERIFY_TOKEN = "verify-me"
ALLOWED_NUMBER = "923001234567"

GET_PATHS = ["/webhook", "/webhook/whatsapp"]
POST_PATHS = ["/webhook", "/webhook/whatsapp"]


def _signature(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    settings = replace(
        web.settings,
        whatsapp_verify_token=VERIFY_TOKEN,
        whatsapp_app_secret=SECRET,
        whatsapp_allowed_numbers=(ALLOWED_NUMBER,),
    )
    monkeypatch.setattr(web, "settings", settings)
    monkeypatch.setattr(whatsapp, "settings", settings)
    db = Database(f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setattr(web, "database", db)
    return TestClient(app), db


@pytest.mark.parametrize("path", GET_PATHS)
def test_get_verification_works_on_both_paths(client, path):
    test_client, _ = client
    response = test_client.get(
        f"{path}?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=12345"
    )
    assert response.status_code == 200
    assert response.text == "12345"


@pytest.mark.parametrize("path", GET_PATHS)
def test_get_verification_rejects_wrong_token_on_both_paths(client, path):
    test_client, _ = client
    response = test_client.get(
        f"{path}?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=12345"
    )
    assert response.status_code == 403


@pytest.mark.parametrize("path", POST_PATHS)
def test_post_status_update_works_on_both_paths(client, path):
    test_client, db = client
    assert db.enqueue_notification(ALLOWED_NUMBER, "key-1", {"text": "hello"})
    notification = db.pending_notifications()[0]
    db.update_notification(notification["id"], "PENDING", external_message_id="wamid.123")
    payload = {
        "entry": [
            {"changes": [{"value": {"statuses": [{"id": "wamid.123", "status": "delivered"}]}}]}
        ]
    }
    body = json.dumps(payload).encode()
    response = test_client.post(
        path, content=body, headers={"X-Hub-Signature-256": _signature(body)}
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    with db.engine.connect() as connection:
        status = connection.exec_driver_sql(
            "SELECT status FROM notifications WHERE external_message_id='wamid.123'"
        ).scalar_one()
    assert status == "DELIVERED"


@pytest.mark.parametrize("path", POST_PATHS)
def test_post_text_message_from_allowed_sender_on_both_paths(client, path, monkeypatch):
    test_client, _ = client
    sent = []
    monkeypatch.setattr(
        whatsapp.WhatsAppClient, "send_text", lambda self, to, body: sent.append((to, body))
    )
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": ALLOWED_NUMBER,
                                    "type": "text",
                                    "text": {"body": "/status"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()
    response = test_client.post(
        path, content=body, headers={"X-Hub-Signature-256": _signature(body)}
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert len(sent) == 1
    assert sent[0][0] == ALLOWED_NUMBER
    assert "RSLR STATUS" in sent[0][1]


@pytest.mark.parametrize("path", POST_PATHS)
def test_post_from_non_allowlisted_sender_is_ignored_on_both_paths(client, path, monkeypatch):
    test_client, _ = client
    sent = []
    monkeypatch.setattr(
        whatsapp.WhatsAppClient, "send_text", lambda self, to, body: sent.append((to, body))
    )
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "9999999999",
                                    "type": "text",
                                    "text": {"body": "/status"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()
    response = test_client.post(
        path, content=body, headers={"X-Hub-Signature-256": _signature(body)}
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert sent == []


@pytest.mark.parametrize("path", POST_PATHS)
def test_post_invalid_signature_rejected_on_both_paths(client, path):
    test_client, _ = client
    body = b'{"object":"whatsapp_business_account"}'
    response = test_client.post(path, content=body, headers={"X-Hub-Signature-256": "sha256=bad"})
    assert response.status_code == 401
    response = test_client.post(path, content=body)
    assert response.status_code == 401


@pytest.mark.parametrize("path", POST_PATHS)
def test_post_malformed_json_rejected_on_both_paths(client, path):
    test_client, _ = client
    body = b"{not json"
    response = test_client.post(
        path, content=body, headers={"X-Hub-Signature-256": _signature(body)}
    )
    assert response.status_code == 400
