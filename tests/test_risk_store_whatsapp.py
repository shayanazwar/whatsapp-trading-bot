import pytest
import hashlib
import hmac

from app.risk import RiskConfig, RiskEngine
from app.storage import Store
from app.strategy.rfpc import TradeCandidate
from app.whatsapp import extract_incoming_messages, sender_key, verify_webhook_signature


def sample_candidate():
    return TradeCandidate(
        candidate_id="CANDIDATE-1", strategy_version="S1-RFPC-0.2.0-1H-trigger", symbol="BTC_USDT",
        side="LONG", decision_time=1704067200, entry_reference=100, stop_price=95, target_price=110,
        initial_risk_distance=5, regime="LONG", setup_pivot_time=1, breakout_time=2,
        reclaim_time=3, trigger_time=4, gross_reward_risk=2.0, reason="test", evidence={},
    )


def test_risk_engine_approves_well_formed_paper_sizing():
    engine = RiskEngine(RiskConfig())
    contract = {"state": 0, "apiAllowed": True, "contractSize": 0.001, "volUnit": 1,
                "minVol": 1, "maxVol": 100000, "priceUnit": 0.01}
    result = engine.evaluate(sample_candidate(), equity=1000, contract=contract)
    assert result.approved is True
    assert result.quantity >= 1
    assert result.modeled_stop_loss <= result.risk_budget + 1e-6


def test_risk_engine_fails_closed_on_missing_or_ineligible_contract():
    engine = RiskEngine()
    assert engine.evaluate(sample_candidate(), equity=100, contract=None).reason == "CONTRACT_METADATA_MISSING"
    contract = {"state": 3, "apiAllowed": True, "contractSize": 1, "volUnit": 1, "minVol": 1, "priceUnit": 0.01}
    assert engine.evaluate(sample_candidate(), equity=100, contract=contract).reason == "CONTRACT_NOT_ENABLED"


def test_risk_engine_respects_kill_switch_and_stale_data():
    engine = RiskEngine()
    assert engine.evaluate(sample_candidate(), equity=1000, contract={}, halt_active=True).reason == "KILL_SWITCH_ACTIVE"
    assert engine.evaluate(sample_candidate(), equity=1000, contract={}, data_is_fresh=False).reason == "STALE_OR_INVALID_DATA"


def test_store_event_and_inbox_are_idempotent(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    first = store.append_event("TEST", {"x": 1}, idempotency_key="same")
    second = store.append_event("TEST", {"x": 2}, idempotency_key="same")
    assert first == second
    payload = {"entry": []}
    assert store.save_inbound("wamid.1", "12345", "STATUS", payload) is True
    assert store.save_inbound("wamid.1", "12345", "STATUS", payload) is False


def test_store_persists_alert_and_durable_outbox(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    alert_id = store.create_alert("BTC_USDT", "price_cross", {"direction": "ABOVE", "level": 70000})
    assert store.list_alerts()[0]["id"] == alert_id
    outbox = store.enqueue_outbox("12345", "alert", idempotency_key="alert-1")
    assert store.enqueue_outbox("12345", "alert duplicate", idempotency_key="alert-1") == outbox
    assert len(store.pending_outbox()) == 1
    store.mark_outbox_unknown(outbox, "timeout")
    assert store.pending_outbox() == []


def test_webhook_hmac_uses_raw_body():
    body = b'{"x":1}'
    secret = "test-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, signature, secret)
    assert not verify_webhook_signature(b'{ "x": 1 }', signature, secret)
    assert not verify_webhook_signature(body, None, secret)


def test_incoming_message_extraction_and_sender_normalization():
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "1"}, "messages": [
        {"id": "wamid.1", "from": "923001234567", "timestamp": "1704067200", "type": "text", "text": {"body": "STATUS"}}
    ]}}]}]}
    messages = extract_incoming_messages(payload)
    assert messages[0]["message_id"] == "wamid.1"
    assert messages[0]["body"] == "STATUS"
    assert sender_key("+92 300 1234567") == "923001234567"


def test_whatsapp_status_updates_are_extracted_separately_from_inbound_messages():
    from app.whatsapp import extract_status_updates

    payload = {
        "entry": [{"changes": [{"value": {"statuses": [{
            "id": "wamid.out.1", "status": "delivered", "recipient_id": "923001234567",
            "timestamp": "1704067200", "pricing": {"billable": True, "category": "service"},
        }]}}]}]
    }
    updates = extract_status_updates(payload)
    assert len(updates) == 1
    assert updates[0]["provider_message_id"] == "wamid.out.1"
    assert updates[0]["status"] == "delivered"
    assert updates[0]["pricing"]["billable"] is True


def test_outbox_acceptance_is_not_mislabelled_as_delivery(tmp_path):
    store = Store(tmp_path / "outbox.sqlite3")
    row_id = store.enqueue_outbox("923001234567", "test", idempotency_key="msg-1")
    store.record_outbox_attempt(row_id, accepted=True, provider_message_id="wamid.out.1")
    with store.connection() as conn:
        row = conn.execute("SELECT status, delivered_at FROM outbox WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "accepted"
    assert row["delivered_at"] is None
    assert store.record_outbox_status("wamid.out.1", "delivered", details={"pricing": {"billable": True}})
    with store.connection() as conn:
        row = conn.execute("SELECT status, delivered_at FROM outbox WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None
    assert store.outbox_month_summary()["delivered"] == 1


def test_alert_crossing_broadcasts_to_all_recipients_once(tmp_path):
    from app.alerts import AlertService

    store = Store(tmp_path / "alert-broadcast.sqlite3")
    service = AlertService(store)
    alert_id = service.create("BTC_USDT", {"kind": "price_cross", "direction": "ABOVE", "level": 100.0})
    service.evaluate_symbol("BTC_USDT", candles_by_interval={}, latest_price=90.0,
                            recipient=["923000000001", "923000000002"])
    fired = service.evaluate_symbol("BTC_USDT", candles_by_interval={}, latest_price=101.0,
                                    recipient=["923000000001", "923000000002"])
    assert len(fired) == 1
    assert fired[0]["id"] == alert_id
    assert fired[0]["recipient_count"] == 2
    pending = store.pending_outbox()
    assert {x["recipient"] for x in pending} == {"923000000001", "923000000002"}
    assert len(pending) == 2
    # A persistent true condition does not fire a second time for either recipient.
    service.evaluate_symbol("BTC_USDT", candles_by_interval={}, latest_price=102.0,
                            recipient=["923000000001", "923000000002"])
    assert len(store.pending_outbox()) == 2


def test_backtest_job_enqueue_is_idempotent_and_worker_claims_once(tmp_path):
    store = Store(tmp_path / "jobs.sqlite3")
    args = {"window": "7D", "strategy_scope": "both"}
    first = store.enqueue_backtest_job("923001234567", args, idempotency_key="wamid.backtest.1")
    duplicate = store.enqueue_backtest_job("923001234567", args, idempotency_key="wamid.backtest.1")
    assert duplicate == first
    claimed = store.claim_next_backtest_job("worker-test")
    assert claimed is not None
    assert claimed["id"] == first
    assert claimed["status"] == "running"
    assert store.claim_next_backtest_job("worker-test-2") is None
    store.finish_backtest_job(first, result={"text": "completed"})
    report = store.get_backtest_job(first, recipient="923001234567")
    assert report["status"] == "complete"
    assert report["result"]["text"] == "completed"


def test_backtest_job_restart_recovery_requeues_running_jobs(tmp_path):
    store = Store(tmp_path / "jobs-recovery.sqlite3")
    job_id = store.enqueue_backtest_job("923001234567", {"window": "1D"}, idempotency_key="restart-1")
    assert store.claim_next_backtest_job("worker-old") is not None
    assert store.requeue_interrupted_backtest_jobs() == 1
    recovered = store.get_backtest_job(job_id)
    assert recovered["status"] == "queued"
    assert recovered["progress"]["stage"] == "recovered_after_restart"


def test_cross_margin_liquidation_price_matches_mexc_single_position_example():
    from app.backtest_rsi_ema import _cross_liquidation_price

    price = _cross_liquidation_price(
        direction="LONG", entry_price=8000.0, unit_quantity=1.0,
        wallet_balance=500.0, maintenance_rate=0.005,
        liquidation_fee_rate=0.0, isolated_margin=0.0,
    )
    assert price == pytest.approx(7540.0, abs=1e-9)


def test_cross_margin_wallet_buffer_moves_liquidation_farther_than_isolated_margin():
    from app.backtest_rsi_ema import _cross_liquidation_price

    cross_price = _cross_liquidation_price(
        direction="LONG", entry_price=100.0, unit_quantity=1.0,
        wallet_balance=100.0, maintenance_rate=0.005,
        liquidation_fee_rate=0.0, isolated_margin=0.0,
    )
    isolated_price = _cross_liquidation_price(
        direction="LONG", entry_price=100.0, unit_quantity=1.0,
        wallet_balance=100.0, maintenance_rate=0.005,
        liquidation_fee_rate=0.0, isolated_margin=1.0,
    )
    assert cross_price < 0 or cross_price < isolated_price


def test_backtest_reconciliation_rejects_corrupted_signal_counts():
    import pytest
    from app.backtest_rsi_ema import _assert_backtest_reconciliation

    with pytest.raises(ArithmeticError, match="signals_direction_sum"):
        _assert_backtest_reconciliation({
            "trade_ledger": [], "signals": 2, "long_signals": 1, "short_signals": 0,
            "signals_entered": 0, "trades_opened": 0, "resolved": 0, "open_unresolved": 0,
            "unfilled_signals": 2, "blocked_signals_due_to_open_position": 0,
            "other_unentered_signals": 2, "total_r": 0.0, "ending_equity": 100.0,
            "starting_equity": 100.0, "net_profit": 0.0, "realized_net_profit": 0.0,
            "unrealized_pnl_at_end": 0.0, "liquidation_exits": 0,
            "take_profit_exits": 0, "stop_loss_exits": 0,
        })
