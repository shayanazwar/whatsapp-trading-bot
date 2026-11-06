from datetime import datetime, timezone

import pytest

from rslr.execution import ExecutionLocked, MEXCExecutionAdapter, PreparedOrder
from rslr.storage import Database


def test_live_order_submission_is_hard_locked():
    adapter = MEXCExecutionAdapter()
    order = PreparedOrder("BTC_USDT", "BUY", 1, 2, 100, 99, 101)
    with pytest.raises(ExecutionLocked, match="hard-coded"):
        adapter.submit_order(order)


def test_signal_storage_deduplicates():
    db = Database("sqlite:///:memory:")
    payload = {
        "symbol": "BTC_USDT", "side": "LONG", "module": "SWEEP",
        "signal_time": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "reference_entry": 100, "stop_loss": 99, "take_profit": 101, "contracts": 1,
    }
    assert db.save_signal(payload) is True
    assert db.save_signal(payload) is False
    assert len(db.latest_signals()) == 1


def test_lock_prevents_second_worker():
    db = Database("sqlite:///:memory:")
    assert db.acquire_lock("scan", "worker-a", 60) is True
    assert db.acquire_lock("scan", "worker-b", 60) is False


def test_lock_can_be_released_by_its_owner():
    db = Database("sqlite:///:memory:")
    assert db.acquire_lock("scan", "worker-a", 3600) is True
    assert db.release_lock("scan", "worker-b") is False
    assert db.release_lock("scan", "worker-a") is True
    assert db.acquire_lock("scan", "worker-b", 3600) is True


def test_whatsapp_delivery_receipt_updates_outbox_state():
    from sqlalchemy import select
    from rslr.storage import NotificationRow

    db = Database("sqlite:///:memory:")
    assert db.enqueue_notification("15550001111", "unique-001", {"body": "RSLR"})
    item = db.pending_notifications()[0]
    db.update_notification(item["id"], "SENT", external_message_id="wamid.test-001")
    assert db.update_notification_by_external_id("wamid.test-001", "delivered")
    with db.Session() as session:
        row = session.scalar(select(NotificationRow).where(NotificationRow.dedupe_key == "unique-001"))
        assert row.status == "DELIVERED"
        assert row.external_message_id == "wamid.test-001"


def test_unknown_delivery_receipt_is_ignored():
    db = Database("sqlite:///:memory:")
    assert db.update_notification_by_external_id("missing", "read") is False
