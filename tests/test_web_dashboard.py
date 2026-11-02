from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app


def test_dashboard_loads_and_api_requires_token(tmp_path):
    cfg = replace(
        load_settings(),
        database_path=tmp_path / "dashboard.sqlite3",
        reports_dir=tmp_path / "reports",
        web_api_token="test-secret-token",
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        page = client.get("/dashboard")
        assert page.status_code == 200
        assert "MEXC Research Dashboard" in page.text
        # Quick actions must execute their command directly; merely filling the field
        # left mobile users staring at the unchanged "Ready" panel.
        assert "runCommand(b.dataset.cmd)" in page.text
        assert "command.addEventListener('keydown'" in page.text
        assert "Waiting for server response" in page.text
        denied = client.post("/api/command", json={"command": "STATUS"})
        assert denied.status_code == 401
        ok = client.post(
            "/api/command",
            json={"command": "STATUS"},
            headers={"Authorization": "Bearer test-secret-token"},
        )
        assert ok.status_code == 200
        assert ok.json()["command"] == "STATUS"
        assert "Live execution: DISABLED" in ok.json()["result"]


def test_web_api_rejects_mutating_commands(tmp_path):
    cfg = replace(
        load_settings(),
        database_path=tmp_path / "dashboard-readonly.sqlite3",
        reports_dir=tmp_path / "reports",
        web_api_token="test-secret-token",
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        response = client.post(
            "/api/command",
            json={"command": "ALERT BTCUSDT ABOVE 90000"},
            headers={"Authorization": "Bearer test-secret-token"},
        )
    assert response.status_code == 403


def test_web_api_is_unavailable_without_token(tmp_path):
    cfg = replace(
        load_settings(),
        database_path=tmp_path / "dashboard-unconfigured.sqlite3",
        reports_dir=tmp_path / "reports",
        web_api_token="",
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        response = client.post("/api/command", json={"command": "STATUS"})
    assert response.status_code == 503


def test_chart_command_returns_authenticated_generated_artifact(tmp_path, monkeypatch):
    from app.services.command_processor import CommandProcessor, CommandResult
    reports = tmp_path / "reports"
    reports.mkdir()
    chart = reports / "chart_BTC_USDT_1H_test.png"
    chart.write_bytes(b"test-chart-png")
    async def fake_execute(self, command):
        return CommandResult("MEXC FUTURES CHART — BTC_USDT · 1H", [(str(chart), "image")], "CHART_RENDERED")
    monkeypatch.setattr(CommandProcessor, "execute", fake_execute)
    cfg = replace(load_settings(), database_path=tmp_path / "attach.sqlite3", reports_dir=reports, web_api_token="secret")
    app = create_app(cfg)
    with TestClient(app) as client:
        response = client.post("/api/command", json={"command": "CHART BTCUSDT 1H"},
                               headers={"Authorization": "Bearer secret"})
        assert response.status_code == 200
        body = response.json()
        assert "CHART" in body["result"]
        assert len(body["attachments"]) == 1
        attachment = body["attachments"][0]
        assert attachment["type"] == "image"
        denied = client.get(attachment["url"])
        assert denied.status_code == 401
        fetched = client.get(attachment["url"], headers={"Authorization": "Bearer secret"})
        assert fetched.status_code == 200
        assert fetched.content == b"test-chart-png"
        assert fetched.headers["content-type"].startswith("image/png")
