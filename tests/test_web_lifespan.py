import logging

from fastapi.testclient import TestClient

from rslr.web import app


def test_app_startup_and_graceful_shutdown(caplog):
    with caplog.at_level(logging.INFO, logger="rslr.web"), TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
    assert "RSLR web starting up" in caplog.text
    assert "RSLR web shutting down" in caplog.text
