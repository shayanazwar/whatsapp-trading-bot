from __future__ import annotations

import hashlib
import hmac
from app.database import Database
from app.models import Candle, ContractInfo
from app.safety import AbnormalMoveGate
from app.sizing import calculate_quantity
from app.whatsapp import WhatsAppClient, extract_inbound_messages, split_backtest_command


def test_database_deduplicates_messages_and_signals(settings):
    db = Database(settings.database_path)
    assert db.mark_message_processed("m1", 100)
    assert not db.mark_message_processed("m1", 101)
    assert db.mark_signal_sent("s1", "LONG", 60, 100)
    assert not db.mark_signal_sent("s1", "LONG", 60, 101)
    db.set("hello", "world", 100)
    assert db.get("hello") == "world"


def test_contract_quantity_rounds_down_to_margin_budget():
    contract = ContractInfo("BTC_USDT", 0.0001, 0.1, 1, 1, 400000, 500, 0, 0.0008, 0.001, 0.0004)
    result = calculate_quantity(80000, contract, 0.50, 200)
    assert result["valid"] is True
    assert result["contracts"] == 12
    assert result["margin"] <= 0.50


def test_quantity_below_minimum_rejected():
    contract = ContractInfo("BTC_USDT", 0.0001, 0.1, 1, 1, 400000, 500, 0, 0.0008, 0.001, 0.0004)
    result = calculate_quantity(80000, contract, 0.00001, 200)
    assert result["valid"] is False


def test_abnormal_gate_pauses_and_recovers(settings):
    db = Database(settings.database_path)
    gate = AbnormalMoveGate(settings, db)
    candles = [Candle(i * 60, 100, 100.1, 99.9, 100, 1) for i in range(6)]
    candles[-1] = Candle(300, 100, 102, 99, 101.5, 1)
    paused, _ = gate.update(candles, 1000)
    assert paused
    normal = [Candle(i * 60, 100, 100.1, 99.9, 100, 1) for i in range(6)]
    paused, _ = gate.update(normal, 1060)
    assert paused
    paused, _ = gate.update(normal, 1960)
    assert not paused


def test_whatsapp_hmac_and_commands(settings):
    secret = "secret"
    custom = settings.__class__(**{**settings.__dict__, "wa_app_secret": secret})
    wa = WhatsAppClient(custom)
    body = b'{"entry":[]}'
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert wa.verify_signature(body, signature)
    assert not wa.verify_signature(body, "sha256=bad")
    assert split_backtest_command("/backtest 30d short") == (30, "SHORT")
    assert split_backtest_command("/backtest 4h long") is None
    messages = extract_inbound_messages({"entry": [{"changes": [{"value": {"messages": [
        {"from": "123", "id": "wamid1", "text": {"body": "/status"}}
    ]}}]}]})
    assert messages == [{"sender": "123", "message_id": "wamid1", "body": "/status"}]


def test_abnormal_gate_does_not_recover_while_one_minute_range_is_still_high(settings):
    db = Database(settings.database_path)
    gate = AbnormalMoveGate(settings, db)
    normal = [Candle(i * 60, 100, 100.1, 99.9, 100, 1) for i in range(6)]
    shock = list(normal)
    shock[-1] = Candle(300, 100, 102, 99, 101.5, 1)
    assert gate.update(shock, 1000)[0]
    assert gate.update(normal, 1060)[0]
    high_range = list(normal)
    high_range[-1] = Candle(300, 100, 101, 99, 100, 1)
    assert gate.update(high_range, 1960)[0]
    assert gate.update(normal, 2860)[0]
    assert not gate.update(normal, 3760)[0]


async def test_whatsapp_send_logs_meta_error_code_on_400(settings, caplog):
    import logging

    import httpx
    import pytest

    custom = settings.__class__(**{**settings.__dict__,
                                    "wa_phone_number_id": "1285387147997440",
                                    "wa_access_token": "test-token"})
    wa = WhatsAppClient(custom)

    def handler(request):
        return httpx.Response(400, json={"error": {"code": 131030,
                                                   "message": "Recipient phone number not in allowed list"}})

    wa._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.ERROR, logger="app.whatsapp"):
        with pytest.raises(RuntimeError, match="HTTP 400"):
            await wa.send_text("923001234567", "hi")
    assert "131030" in caplog.text
    assert "Recipient phone number not in allowed list" in caplog.text
