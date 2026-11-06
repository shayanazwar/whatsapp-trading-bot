import hashlib
import hmac
import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from rslr import web, whatsapp
from rslr.jobs import process_backtest_jobs
from rslr.storage import Database
from rslr.web import app

SECRET = "test-secret"
VERIFY_TOKEN = "verify-me"
ALLOWED_NUMBER = "923001234567"


def _signature(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payload_for(sender: str, text: str) -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"from": sender, "type": "text", "text": {"body": text}}
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()


def _fake_backtest_payload() -> dict:
    return {
        "period": "1D",
        "starting_equity": 100.0,
        "coins_tested": 3,
        "total_signals": 2,
        "trades_taken": 2,
        "long_signals": 1,
        "short_signals": 1,
        "tp_hits": 1,
        "sl_hits": 1,
        "open_unresolved": 0,
        "expired": 0,
        "tp_hit_rate_pct": 50.0,
        "net_pnl_usd": 1.25,
        "net_return_pct": 1.25,
        "max_drawdown_pct": 0.5,
        "profit_factor": 2.0,
        "average_r": 0.5,
        "total_fees_usd": 0.01,
        "total_slippage_usd": 0.005,
        "funding_pnl_usd": 0.0,
        "sweep_signals": 2,
        "sweep_net_pnl": 1.25,
        "donchian_signals": 0,
        "donchian_net_pnl": 0.0,
        "trades": [],
    }


class _FakeResult:
    def to_dict(self) -> dict:
        return _fake_backtest_payload()


class _FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to: str, body: str) -> dict:
        self.sent.append((to, body))
        return {"messages": [{"id": "wamid.test"}]}


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
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        whatsapp.WhatsAppClient, "send_text", lambda self, to, body: sent.append((to, body))
    )
    return TestClient(app), db, sent


def test_help_command_replies_immediately(client):
    test_client, _, sent = client
    body = _payload_for(ALLOWED_NUMBER, "/help")
    response = test_client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _signature(body)}
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert len(sent) == 1
    assert sent[0][0] == ALLOWED_NUMBER
    assert "RSLR COMMANDS" in sent[0][1]
    assert "/backtest" in sent[0][1]


def test_backtest_command_creates_job_and_replies_immediately(client):
    test_client, db, sent = client
    body = _payload_for(ALLOWED_NUMBER, "/backtest 30D")
    response = test_client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _signature(body)}
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert len(sent) == 1
    assert "queued" in sent[0][1]
    assert "job #" in sent[0][1]
    job = db.get_backtest_job(1)
    assert job is not None
    assert job["period"] == "30D"
    assert job["sender"] == ALLOWED_NUMBER
    assert job["status"] == "PENDING"


def test_backtest_command_rejects_bad_period_without_creating_job(client):
    test_client, db, sent = client
    body = _payload_for(ALLOWED_NUMBER, "/backtest 99D")
    response = test_client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _signature(body)}
    )
    assert response.status_code == 200
    assert "Use: /backtest 1D, /backtest 7D or /backtest 30D" in sent[0][1]
    assert db.get_backtest_job(1) is None


def test_background_job_completion_stores_real_result_and_reports(client):
    _, db, _ = client
    job_id = db.enqueue_backtest_job("1D", ALLOWED_NUMBER)
    fake_whatsapp = _FakeWhatsApp()
    processed = process_backtest_jobs(
        db, whatsapp=fake_whatsapp, backtest_fn=lambda period: _FakeResult()
    )
    assert processed == 1
    job = db.get_backtest_job(job_id)
    assert job["status"] == "DONE"
    assert job["backtest_run_id"] is not None
    assert "BACKTEST" in job["result_summary"]
    saved = db.latest_backtest()
    assert saved is not None
    assert saved["period"] == "1D"
    assert saved["net_pnl_usd"] == 1.25
    assert len(fake_whatsapp.sent) == 1
    assert fake_whatsapp.sent[0][0] == ALLOWED_NUMBER
    assert "job #1" in fake_whatsapp.sent[0][1]
    assert "Net P/L: $+1.25" in fake_whatsapp.sent[0][1]


def test_background_job_failure_reports_actual_error(client):
    _, db, _ = client
    job_id = db.enqueue_backtest_job("7D", ALLOWED_NUMBER)
    fake_whatsapp = _FakeWhatsApp()

    def _boom(period: str):
        raise RuntimeError("MEXC unavailable")

    processed = process_backtest_jobs(db, whatsapp=fake_whatsapp, backtest_fn=_boom)
    assert processed == 1
    job = db.get_backtest_job(job_id)
    assert job["status"] == "FAILED"
    assert "MEXC unavailable" in job["error"]
    assert len(fake_whatsapp.sent) == 1
    assert "failed" in fake_whatsapp.sent[0][1]
    assert "MEXC unavailable" in fake_whatsapp.sent[0][1]


def test_whatsapp_api_error_does_not_fail_webhook(client, monkeypatch):
    test_client, _, _ = client

    def _failing_send(self, to, body):
        raise RuntimeError("Graph API 500")

    monkeypatch.setattr(whatsapp.WhatsAppClient, "send_text", _failing_send)
    body = _payload_for(ALLOWED_NUMBER, "/status")
    response = test_client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _signature(body)}
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": True}


def test_web_process_runs_queued_backtest_job(client, monkeypatch):
    test_client, db, sent = client
    monkeypatch.setattr("rslr.jobs.run_backtest", lambda period: _FakeResult())
    job_id = db.enqueue_backtest_job("1D", ALLOWED_NUMBER)
    with test_client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = db.get_backtest_job(job_id)
            if job and job["status"] == "DONE":
                break
            time.sleep(0.1)
    job = db.get_backtest_job(job_id)
    assert job["status"] == "DONE"
    assert job["backtest_run_id"] is not None
    assert any("job #1" in body for _, body in sent)
