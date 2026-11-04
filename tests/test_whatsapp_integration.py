from __future__ import annotations

import hashlib
import hmac
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.notifications.whatsapp import (
    WhatsAppAPIError,
    WhatsAppCloudAPI,
    WhatsAppConfigurationError,
)
from app.notifications import whatsapp_commands as commands


def settings(**overrides):
    values = {
        "whatsapp_enabled": True,
        "whatsapp_access_token": "test-token",
        "whatsapp_phone_number_id": "123456789012345",
        "whatsapp_verify_token": "verify-me",
        "whatsapp_app_secret": "app-secret",
        "whatsapp_graph_api_version": "v26.0",
        "whatsapp_api_timeout_seconds": 5.0,
        "whatsapp_api_max_retries": 0,
        "whatsapp_default_recipient": "923001234567",
        "whatsapp_allowed_recipients": "923001234567",
        "public_base_url": "https://example.onrender.com",
        "data_dir": __import__("pathlib").Path("/tmp/kronos-test-data"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def mock_response(status_code=200, body=None):
    response = Mock()
    response.status_code = status_code
    response.headers = {}
    response.text = ""
    response.json.return_value = body or {"messages": [{"id": "wamid.test"}]}
    return response


def test_whatsapp_status_and_text_sender_use_cloud_api():
    client = Mock()
    client.post.return_value = mock_response()
    adapter = WhatsAppCloudAPI(settings=settings(), client=client)

    result = adapter.send_text("+923001234567", "hello from tests")

    assert result["messages"][0]["id"] == "wamid.test"
    args, kwargs = client.post.call_args
    assert args[0] == "https://graph.facebook.com/v26.0/123456789012345/messages"
    assert kwargs["json"]["to"] == "923001234567"
    assert kwargs["json"]["text"]["body"] == "hello from tests"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert adapter.status().enabled is True
    assert adapter.status().webhook_configured is True
    assert adapter.status().recipient_allowlist_configured is True
    assert adapter.status().recipient_allowlist_count == 1


def test_whatsapp_sender_fails_closed_when_disabled_or_missing_credentials():
    disabled = WhatsAppCloudAPI(settings=settings(whatsapp_enabled=False), client=Mock())
    assert disabled.status().enabled is False
    with pytest.raises(WhatsAppConfigurationError, match="disabled"):
        disabled.send_text("923001234567", "test")

    missing = WhatsAppCloudAPI(settings=settings(whatsapp_phone_number_id=""), client=Mock())
    assert missing.status().configured is False
    with pytest.raises(WhatsAppConfigurationError):
        missing.send_text("923001234567", "test")


def test_direct_crypto_signal_delivery_is_blocked_from_whatsapp():
    adapter = WhatsAppCloudAPI(settings=settings(), client=Mock())
    with pytest.raises(WhatsAppConfigurationError, match="not sent via WhatsApp"):
        adapter.send_crypto_signal({"symbol": "BTC_USDT", "side": "LONG", "entry": 100, "stop": 95, "tp1": 110})


def test_whatsapp_sender_rejects_invalid_phone_and_non_https_chart():
    adapter = WhatsAppCloudAPI(settings=settings(), client=Mock())
    with pytest.raises(ValueError, match="8 to 15 digits"):
        adapter.send_text("12", "hello")
    with pytest.raises(ValueError, match="HTTPS"):
        adapter.send_image("923001234567", "http://example.com/chart.png")


def test_whatsapp_api_surfaces_non_retryable_error():
    client = Mock()
    client.post.return_value = mock_response(400, {"error": {"message": "bad recipient"}})
    adapter = WhatsAppCloudAPI(settings=settings(), client=client)
    with pytest.raises(WhatsAppAPIError, match="bad recipient"):
        adapter.send_text("923001234567", "hello")


def test_webhook_signature_is_verified():
    raw = b'{"object":"whatsapp_business_account"}'
    secret = "test-secret"
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    assert commands.verify_webhook_signature(raw, signature, secret)
    assert not commands.verify_webhook_signature(raw, "sha256=bad", secret)
    assert not commands.verify_webhook_signature(raw, signature, "")


def test_backtest_commands_queue_1d_7d_and_30d(monkeypatch):
    queued = []

    def enqueue(job_type, payload):
        job = SimpleNamespace(id=f"job-{payload['days']}", job_type=job_type,
                              payload=payload, status="queued", progress=0,
                              progress_message="Queued", error=None, result=None)
        queued.append((job_type, payload))
        return job

    monkeypatch.setattr(commands, "enqueue_job", enqueue)
    assert "1D backtest queued" in commands.dispatch_command("923001234567", "BACKTEST 1D")
    assert "7D backtest queued" in commands.dispatch_command("923001234567", "BACKTEST BTCUSDT 7D")
    assert "30D backtest queued" in commands.dispatch_command("923001234567", "BACKTEST ETHUSDT 30D")
    assert [item[1]["days"] for item in queued] == [1, 7, 30]
    assert [item[1]["symbol"] for item in queued] == ["BTC_USDT", "BTC_USDT", "ETH_USDT"]
    assert all(item[1]["whatsapp_recipient"] == "923001234567" for item in queued)


def test_help_includes_real_command_names():
    help_text = commands.dispatch_command("923001234567", "HELP")
    for command in ("PING", "ANALYZE", "FORECAST", "BACKTEST", "BACKTEST STATUS", "STATUS", "SCAN", "SIGNALS", "DASHBOARD"):
        assert command in help_text
    assert "entries, stops" in help_text


def test_chart_renderer_creates_png(tmp_path):
    frame = pd.DataFrame([
        {"open": 100 + i, "high": 103 + i, "low": 98 + i, "close": 101 + i}
        for i in range(30)
    ])
    target = tmp_path / "chart.png"
    commands._render_candlestick_chart(frame, "BTC_USDT", "4H", target)
    assert target.exists()
    assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_webhook_event_is_deduplicated_and_only_allowlisted_sender_runs(monkeypatch):
    monkeypatch.setattr(commands, "allowed_senders", lambda: {"923001234567"})
    sent = []
    monkeypatch.setattr(commands, "dispatch_command", lambda sender, text, base: f"reply:{text}")
    monkeypatch.setattr(commands, "_send_text", lambda sender, text: sent.append((sender, text)))
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.unique-test",
                        "from": "923001234567",
                        "type": "text",
                        "text": {"body": "HELP"},
                    }]
                }
            }]
        }]
    }
    commands.process_whatsapp_payload(payload, "https://example.onrender.com")
    commands.process_whatsapp_payload(payload, "https://example.onrender.com")
    assert sent == [("923001234567", "reply:HELP")]


def test_whatsapp_webhook_routes_verify_and_accept_signed_post(monkeypatch):
    from app import main
    from app.api import routes

    cfg = settings()
    monkeypatch.setattr(routes, "get_settings", lambda: cfg)
    captured = []
    monkeypatch.setattr(
        routes, "process_whatsapp_payload",
        lambda payload, base: captured.append((payload, base)),
    )
    client = TestClient(main.app)

    get_response = client.get(
        "/api/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-value",
        },
    )
    assert get_response.status_code == 200
    assert get_response.text == "challenge-value"

    raw = b'{"object":"whatsapp_business_account","entry":[]}'
    signature = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    post_response = client.post(
        "/api/whatsapp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )
    assert post_response.status_code == 200
    assert post_response.json()["accepted"] is True
    assert captured and captured[0][0]["object"] == "whatsapp_business_account"

    alias_get = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "alias-challenge",
        },
    )
    assert alias_get.status_code == 200
    assert alias_get.text == "alias-challenge"

    alias_post = client.post(
        "/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )
    assert alias_post.status_code == 200
    assert alias_post.json()["accepted"] is True


def test_price_and_chart_commands_route_users_to_dashboard():
    message = commands.dispatch_command("923001234567", "PRICE BTCUSDT")
    assert "authenticated dashboard" in message
    chart_message = commands.dispatch_command("923001234567", "CHART BTCUSDT 4H")
    assert "authenticated dashboard" in chart_message


def test_forecast_command_queues_pinned_forecast_job(monkeypatch):
    captured = {}
    job = SimpleNamespace(id="forecast-123")
    cfg = settings(forecast_sample_count=1000, forecast_horizon=24)
    monkeypatch.setattr(commands, "get_settings", lambda: cfg)

    def enqueue(job_type, payload):
        captured["job_type"] = job_type
        captured["payload"] = payload
        return job

    monkeypatch.setattr(commands, "enqueue_job", enqueue)
    reply = commands.dispatch_command("923001234567", "FORECAST BTCUSDT")
    assert captured["job_type"] == "forecast"
    assert captured["payload"]["symbol"] == "BTC_USDT"
    assert captured["payload"]["samples"] == 1000
    assert captured["payload"]["horizon"] == 24
    assert "forecast-123" in reply


def test_signals_command_only_links_to_authenticated_dashboard(monkeypatch):
    monkeypatch.setattr(commands, "get_settings", lambda: settings())
    reply = commands.dispatch_command("923001234567", "SIGNALS")
    assert "https://example.onrender.com/" in reply
    assert "authenticated dashboard" in reply
    assert "Entry:" not in reply and "Stop:" not in reply


def test_analyze_command_queues_existing_analysis_worker(monkeypatch):
    captured = {}
    job = SimpleNamespace(id="analysis-123")

    def enqueue(job_type, payload):
        captured["job_type"] = job_type
        captured["payload"] = payload
        return job

    monkeypatch.setattr(commands, "enqueue_job", enqueue)
    reply = commands.dispatch_command("923001234567", "ANALYZE ETHUSDT")
    assert captured["job_type"] == "analysis"
    assert captured["payload"]["symbol"] == "ETH_USDT"
    assert captured["payload"]["whatsapp_notify_on_completion"] is True
    assert "analysis-123" in reply


def test_completion_notification_reports_analysis_result(monkeypatch):
    sent = []

    class FakeAdapter:
        def send_text(self, recipient, text):
            sent.append((recipient, text))
            return {"messages": [{"id": "wamid.test"}]}
        def close(self):
            pass

    monkeypatch.setattr(commands, "WhatsAppCloudAPI", FakeAdapter)
    job = SimpleNamespace(
        id="analysis-123",
        job_type="analysis",
        payload={"whatsapp_notify_on_completion": True, "whatsapp_recipient": "923001234567"},
    )
    commands.notify_job_completion(job, result={
        "symbol": "BTC_USDT",
        "decision": "WAIT",
        "market": {"last_price": 67000},
        "strategy": {"side": "LONG", "score": 75, "entry": 67000, "stop": 66000, "tp1": 68000, "tp2": 69500},
        "reasons": ["test reason"],
    })
    assert sent
    assert sent[0][0] == "923001234567"
    assert "KRONOS ANALYSIS JOB COMPLETED" in sent[0][1]
    assert "authenticated dashboard" in sent[0][1]
    for forbidden in ("Entry:", "Stop:", "TP1:", "TP2:", "LONG", "67000"):
        assert forbidden not in sent[0][1]


def test_webhook_rejects_invalid_signature(monkeypatch):
    from app import main
    from app.api import routes

    monkeypatch.setattr(routes, "get_settings", lambda: settings())
    client = TestClient(main.app)
    response = client.post(
        "/api/whatsapp/webhook",
        content=b'{"object":"whatsapp_business_account"}',
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=invalid"},
    )
    assert response.status_code == 401



def test_backtest_worker_accepts_one_day_payload(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.services import pipeline

    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    captured = {}

    class FakeDB:
        def add(self, _row):
            pass

    class FakeContext:
        def __enter__(self):
            return FakeDB()
        def __exit__(self, *_):
            return False

    class FakeSessionFactory:
        @staticmethod
        def begin():
            return FakeContext()

    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(
        backtest_max_days=180,
        backtest_default_equity=1000.0,
        universe_limit=30,
        forecast_sample_count=1000,
    ))
    monkeypatch.setattr(pipeline, "_utc_now", lambda: now)
    monkeypatch.setattr(pipeline, "update_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "SessionLocal", FakeSessionFactory)
    monkeypatch.setattr(pipeline, "_run_symbol_backtest", lambda **kwargs: {
        "symbol": kwargs["symbol"], "trades": [], "unresolved_positions": [],
        "metrics": {}, "equity_curve": [], "warnings": [],
    })
    monkeypatch.setattr(pipeline, "_aggregate_sleeve_equity_curves", lambda *args, **kwargs: [])
    monkeypatch.setattr(pipeline, "_metrics", lambda *args, **kwargs: {})
    result = pipeline.run_backtest_job("job-one-day", {"symbol": "BTCUSDT", "days": 1})

    assert result["symbol"] == "BTC_USDT"
    assert datetime.fromisoformat(result["start_time"]) == now - timedelta(days=1)
    assert datetime.fromisoformat(result["end_time"]) == now

def test_backtest_api_request_accepts_one_day():
    from app.api.routes import BacktestRequest
    assert BacktestRequest(symbol="BTCUSDT", days=1).days == 1



def test_whatsapp_chart_route_serves_only_valid_png_ids(monkeypatch, tmp_path):
    from app import main
    from app.api import routes

    chart_dir = tmp_path / "whatsapp_charts"
    chart_dir.mkdir()
    chart_id = "a" * 32
    png_path = chart_dir / f"{chart_id}.png"
    png_path.write_bytes(b"\\x89PNG\\r\\n\\x1a\\nchart")
    expired_id = "c" * 32
    expired_path = chart_dir / f"{expired_id}.png"
    expired_path.write_bytes(b"\\x89PNG\\r\\n\\x1a\\nold")
    os.utime(expired_path, (1, 1))
    cfg = settings(data_dir=tmp_path)
    monkeypatch.setattr(routes, "get_settings", lambda: cfg)
    client = TestClient(main.app)

    ok = client.get(f"/api/whatsapp/charts/{chart_id}.png")
    invalid = client.get("/api/whatsapp/charts/not-a-valid-id.png")
    missing = client.get(f"/api/whatsapp/charts/{'b' * 32}.png")
    expired = client.get(f"/api/whatsapp/charts/{expired_id}.png")

    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/png")
    assert ok.content.startswith(b"\\x89PNG")
    assert invalid.status_code == 404
    assert missing.status_code == 404
    assert expired.status_code == 404
    assert not expired_path.exists()

def test_graph_api_retries_transient_429(monkeypatch):
    responses = [mock_response(429, {"error": {"message": "slow down"}}), mock_response()]
    client = Mock()
    client.post.side_effect = responses
    cfg = settings(whatsapp_api_max_retries=1)
    monkeypatch.setattr("app.notifications.whatsapp.time.sleep", lambda *_: None)
    adapter = WhatsAppCloudAPI(settings=cfg, client=client)
    result = adapter.send_text("923001234567", "retry me")
    assert result["messages"][0]["id"] == "wamid.test"
    assert client.post.call_count == 2
