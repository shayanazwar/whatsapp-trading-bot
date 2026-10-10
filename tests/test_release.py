from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.analysis.engine import APPROVED_TIMEFRAMES, TIMEFRAME_ALIASES as ENGINE_TIMEFRAME_ALIASES, synthesize_12h_from_4h
from app.backtest.report import format_report, summarize
from app.backtest.runner import BacktestAlreadyRunning, BacktestRunner
from app.backtest.simulator import simulate_trade
from app.bot import Bot
from app.charts import ChartRenderer
from app.config import Settings
from app.database import Database
from app.market import MarketData, MarketRef, TIMEFRAME_ALIASES
from app.automation.mexc_client import MexcClient
from app.automation.signal_validator import make_signal_key, validate_signal


def candle(ts: int, close: float, *, spread: float = 2.0, volume: float = 100.0) -> list[float | int]:
    return [ts, close - spread / 2, close + spread / 2, close - spread, close, volume]


def test_only_approved_timeframes_are_exposed():
    assert APPROVED_TIMEFRAMES == ("1D", "12H", "4H", "1H")
    assert set(ENGINE_TIMEFRAME_ALIASES) == {"1H", "1HR", "1HOUR", "4H", "4HR", "4HOUR", "12H", "12HR", "12HOUR", "1D", "1DAY"}


def test_12h_is_causally_aggregated_from_contiguous_4h():
    base = (1_700_000_000_000 // 43_200_000) * 43_200_000
    rows = [candle(base + i * 14_400_000, 100 + i) for i in range(3)]
    rows.append(candle(base + 3 * 14_400_000, 103))
    out = synthesize_12h_from_4h(rows, now_ms=base + 12 * 3_600_000 + 1)
    assert len(out) == 1
    assert out[0]["open"] == rows[0][1]
    assert out[0]["close"] == rows[2][4]
    assert out[0]["volume"] == sum(row[5] for row in rows[:3])


def test_mexc_client_rejects_unsupported_candle_intervals():
    client = MexcClient(Settings())
    try:
        with pytest.raises(ValueError):
            asyncio.run(client.get_klines("BTC_USDT", "Min" + "15", 10))
    finally:
        asyncio.run(client.close())


def test_mexc_contract_discovery_uses_documented_futures_endpoint():
    client = MexcClient(Settings())
    class MockTransport:
        async def __call__(self, request):
            assert request.url.path == "/api/v1/contract/detail"
            return httpx.Response(200, json={"success": True, "data": []})
    import httpx
    try:
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(MockTransport()))
        assert asyncio.run(client.get_contracts()) == []
    finally:
        asyncio.run(client.close())


def test_market_12h_uses_4h_source():
    class FakeClient:
        async def get_klines(self, symbol, interval, limit):
            assert interval == "Hour4"
            assert limit >= 9
            base = (1_700_000_000_000 // 43_200_000) * 43_200_000
            return [candle(base + i * 14_400_000, 100 + i) for i in range(12)]

        async def get_contracts(self):
            return [{"symbol": "BTC_USDT", "state": 0, "isHidden": False, "preMarket": False, "quoteCoin": "USDT", "settleCoin": "USDT", "futureType": 1}]

    market = MarketData(Settings(), client=FakeClient())
    out = asyncio.run(market.ohlcv(MarketRef("mexc", "BTC_USDT"), "12H", 2))
    assert len(out) == 2


def valid_signal_data() -> dict:
    now = int(time.time() * 1000)
    return {
        "symbol": "BTC_USDT",
        "setup": "LONG",
        "candle_time": now - 10 * 1_000,
        "entry_time": now - 10 * 1_000,
        "entry_mode": "MARKET",
        "setup_bos_time": now - 2 * 3_600_000,
        "bos_4h_level": 100.0,
        "score": 92,
        "direction_ok": True,
        "structure_ok": True,
        "setup_ok": True,
        "confirmation_ok": True,
        "location_ok": True,
        "target_path_structural": True,
        "structure_quality_ok": True,
        "shock_veto_ok": True,
        "technical_candidate": True,
        "trade_geometry_ok": True,
        "risk_ok": True,
        "primary_entry_timeframe": "1H",
        "signal_candle_timeframe": "1H",
        "entry": 110.0,
        "stop_loss": 105.0,
        "tp": 128.0,
        "rr": 3.6,
        "atr": 4.0,
        "sl_atr": 1.2,
        "tp_distance_atr": 4.5,
        "mexc_spread_pct": 0.01,
    }


def test_signal_validator_accepts_1h_and_keys_4h_setup():
    data = valid_signal_data()
    signal, reasons = validate_signal(data, min_confluence=78, min_rr=2.0)
    assert not reasons
    assert signal is not None
    assert signal.analysis["primary_entry_timeframe"] == "1H"
    assert signal.key == make_signal_key("BTC_USDT", "LONG", data["candle_time"], setup_bos_time=data["setup_bos_time"], bos_level=data["bos_4h_level"])


def test_signal_key_changes_when_structural_setup_changes():
    a = make_signal_key("BTC_USDT", "LONG", setup_bos_time=1_700_000_000_000, bos_level=100.0)
    b = make_signal_key("BTC_USDT", "LONG", setup_bos_time=1_700_000_003_600_000, bos_level=100.0)
    c = make_signal_key("BTC_USDT", "LONG", setup_bos_time=1_700_000_000_000, bos_level=101.0)
    assert a != b
    assert a != c


def test_trade_geometry_is_single_tp_and_1h_simulation():
    signal = valid_signal_data()
    signal["position_size"] = 1.0
    signal["contract_size"] = 1.0
    close_time = signal["candle_time"] + 3_600_000
    future = [[close_time, 110.0, 129.0, 109.0, 125.0, 100.0]]
    trade = simulate_trade(signal, future, signal_close_time_ms=close_time, max_holding_minutes=180)
    assert trade is not None
    assert trade.outcome == "TP"
    assert trade.tp1 == trade.tp2 == 128.0


def test_zero_trade_backtest_report_is_always_rendered():
    summary = summarize(
        days=1,
        period_start_ms=1_700_000_000_000,
        period_end_ms=1_700_086_400_000,
        coins_selected=10,
        coins_tested=10,
        data_errors=0,
        execution_errors=0,
        rejected_setups=10,
        trades=[],
        diagnostics={"NO_VALID_SETUP": 10},
    )
    report = format_report(summary)
    assert "Signals: 0" in report
    assert "Rejected Setups: 10" in report
    assert "GATE DIAGNOSTICS" not in report


@pytest.mark.parametrize("days", [1, 7])
def test_backtest_runner_empty_universe_returns_report(days):
    class EmptyUniverse:
        async def refresh(self):
            return []

    runner = BacktestRunner(client=None, universe=EmptyUniverse(), settings=Settings(), max_concurrency=1)
    summary = asyncio.run(runner.run(days))
    assert summary.days == days
    assert summary.signals == 0
    assert "Signals: 0" in format_report(summary)


def test_backtest_runner_duplicate_job_guard():
    class EmptyUniverse:
        async def refresh(self):
            return []

    runner = BacktestRunner(client=None, universe=EmptyUniverse(), settings=Settings(), max_concurrency=1)
    runner._running = True
    try:
        with pytest.raises(BacktestAlreadyRunning):
            asyncio.run(runner.run(1))
    finally:
        runner._running = False


def test_bot_commands_are_whatsapp_only(tmp_path: Path):
    class FakeWhatsApp:
        def __init__(self):
            self.texts = []
        async def send_text(self, to, body):
            self.texts.append((to, body))
        async def upload_image(self, path):
            return "media-id"
        async def send_image(self, to, media_id, caption=None):
            return {"messages": [{"id": "image"}]}

    class FakeMarket:
        async def resolve(self, raw):
            return MarketRef("mexc", raw.upper().replace("/", "_"))
        async def price(self, symbol):
            return 1234.56

    wa = FakeWhatsApp()
    bot = Bot(Settings(allowed_users="923001234567"), Database(str(tmp_path / "bot.sqlite3")), FakeMarket(), wa, ChartRenderer())
    asyncio.run(bot.handle("923001234567", "PRICE BTCUSDT"))
    assert wa.texts[-1][1].startswith("💰 BTCUSDT")
    asyncio.run(bot.handle("923001234567", "HELP"))
    assert "BACKTEST 1D" in wa.texts[-1][1]
    assert "12H" in wa.texts[-1][1]
    assert "15M Structure" not in wa.texts[-1][1]


def test_whatsapp_signature_verification():
    from app.whatsapp import WhatsAppClient
    import hashlib
    import hmac

    client = WhatsAppClient("TOKEN", "123", "v26.0", "SECRET")
    body = b'{"object":"whatsapp_business_account"}'
    expected = "sha256=" + hmac.new(b"SECRET", body, hashlib.sha256).hexdigest()
    try:
        assert client.verify_signature(body, expected)
        assert not client.verify_signature(body, expected.upper())
        assert not client.verify_signature(body, None)
    finally:
        asyncio.run(client.close())


def test_whatsapp_send_text_posts_to_messages_endpoint_and_returns_message_id():
    import httpx
    from app.whatsapp import WhatsAppClient

    requests = []
    async def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v26.0/123/messages"
        assert request.headers["Authorization"] == "Bearer TOKEN"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["messaging_product"] == "whatsapp"
        assert payload["to"] == "923001234567"
        assert payload["text"]["body"] == "HELP"
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    client = WhatsAppClient("TOKEN", "123", "v26.0", "SECRET")
    asyncio.run(client.http.aclose())
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = asyncio.run(client.send_text("923001234567", "HELP"))
        assert response["messages"][0]["id"] == "wamid.out"
        assert len(requests) == 1
    finally:
        asyncio.run(client.close())


def test_webhook_ignores_unexpected_phone_number_id(monkeypatch):
    import app.main as main

    class FakeDB:
        def is_message_seen(self, message_id):
            return False
        def mark_message_seen(self, message_id):
            raise AssertionError("Unexpected event should be ignored")

    class FakeBot:
        async def handle(self, sender, text):
            raise AssertionError("Unexpected event should not reach command router")

    class FakeSettings:
        meta_phone_number_id = "expected"

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": "other"}, "messages": [{"id": "wamid.other", "from": "923001234567", "type": "text", "text": {"body": "HELP"}}]}}]}],
    }
    monkeypatch.setattr(main, "db", FakeDB())
    monkeypatch.setattr(main, "bot", FakeBot())
    monkeypatch.setattr(main, "settings", FakeSettings())
    main._processing_message_ids.clear()
    asyncio.run(main.process_webhook(payload))


def test_webhook_processing_deduplicates_after_success_and_retries_after_failure(monkeypatch):
    import app.main as main

    class FakeDB:
        def __init__(self):
            self.seen = set()
            self.marked = []
        def is_message_seen(self, message_id):
            return message_id in self.seen
        def mark_message_seen(self, message_id):
            self.marked.append(message_id)
            if message_id in self.seen:
                return False
            self.seen.add(message_id)
            return True

    class FakeBot:
        def __init__(self):
            self.calls = 0
        async def handle(self, sender, text):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": "123"}, "messages": [{"id": "wamid.test", "from": "923001234567", "type": "text", "text": {"body": "HELP"}}]}}]}],
    }
    db = FakeDB()
    bot = FakeBot()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "bot", bot)
    main._processing_message_ids.clear()
    asyncio.run(main.process_webhook(payload))
    assert db.marked == []
    asyncio.run(main.process_webhook(payload))
    assert db.marked == ["wamid.test"]
    asyncio.run(main.process_webhook(payload))
    assert bot.calls == 2
