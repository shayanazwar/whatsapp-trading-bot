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
        assert "Waiting for the server response" in page.text
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
