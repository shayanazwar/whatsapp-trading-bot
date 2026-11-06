from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
from fastapi.testclient import TestClient

from rslr.storage import Database
from rslr.web import app


def test_health_endpoint_reports_database_and_hard_locked_execution():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["live_execution"] is False
    assert response.json()["database"] is True


def test_meta_get_webhook_verification_uses_dotted_query_names(monkeypatch):
    import rslr.web as web
    monkeypatch.setattr(web, "settings", replace(web.settings, whatsapp_verify_token="verify-me"))
    response = TestClient(app).get(
        "/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=12345"
    )
    assert response.status_code == 200
    assert response.text == "12345"


def test_candle_and_funding_persistence_is_idempotent():
    db = Database("sqlite:///:memory:")
    timestamp = pd.Timestamp("2026-01-01T00:00:00Z")
    frame = pd.DataFrame([{
        "timestamp": timestamp, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 12,
    }])
    db.save_candles("BTC_USDT", "1H", frame)
    db.save_candles("BTC_USDT", "1H", frame)
    funding = pd.DataFrame([{"timestamp": timestamp, "funding_rate": 0.0001}])
    db.save_funding("BTC_USDT", funding)
    db.save_funding("BTC_USDT", funding)
    with db.engine.connect() as connection:
        candles = connection.exec_driver_sql("SELECT COUNT(*) FROM candles").scalar_one()
        rates = connection.exec_driver_sql("SELECT COUNT(*) FROM funding_history").scalar_one()
    assert candles == 1
    assert rates == 1


def test_paper_position_created_from_saved_signal_and_closed_as_outcome():
    db = Database("sqlite:///:memory:")
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    signal = {
        "symbol": "BTC_USDT", "side": "LONG", "module": "SWEEP", "signal_time": stamp.isoformat(),
        "reference_entry": 100, "stop_loss": 99, "take_profit": 101, "contracts": 2,
    }
    assert db.save_signal(signal)
    positions = db.open_paper_positions()
    assert len(positions) == 1
    db.close_paper_position(positions[0]["id"], "TP", 101, stamp, 0.0)
    assert db.open_paper_positions() == []


def test_paper_tracking_can_be_disabled(monkeypatch):
    from dataclasses import replace
    import rslr.storage as storage_module
    from rslr.config import settings
    from rslr.storage import Database
    from datetime import datetime, timezone

    monkeypatch.setattr(storage_module, "settings", replace(settings, paper_trading_enabled=False))
    db = Database("sqlite:///:memory:")
    payload = {
        "symbol": "BTC_USDT", "side": "LONG", "module": "SWEEP",
        "signal_time": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "reference_entry": 100, "stop_loss": 99, "take_profit": 101, "contracts": 1,
    }
    assert db.save_signal(payload) is True
    assert db.open_paper_positions() == []
