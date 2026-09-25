from pathlib import Path

from app.database import Database


def test_alert_lifecycle(tmp_path: Path):
    db = Database(str(tmp_path / "test.sqlite3"))
    alert = db.create_alert("923001234567", "mexc", "BTC_USDT", "above", 100000)
    assert alert.id == 1
    assert db.list_alerts("923001234567")[0].target == 100000
    assert db.deactivate(alert.id, "923001234567") is True
    assert db.list_alerts("923001234567") == []
    assert db.deactivate(alert.id, "923001234567") is False


def test_message_dedupe(tmp_path: Path):
    db = Database(str(tmp_path / "test.sqlite3"))
    assert db.mark_message_seen("m1") is True
    assert db.mark_message_seen("m1") is False
