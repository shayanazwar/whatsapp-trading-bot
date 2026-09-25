from __future__ import annotations

import asyncio

from app.analysis.engine import calculate_confluence, closed_candle_rows
from app.automation.executor import ExecutionResult, MexcExecutor, build_limit_order_payload
from app.automation.mexc_client import MexcClient, build_query_string, build_signature
from app.automation.risk_manager import TradePlan, calculate_contract_quantity, validate_levels
from app.automation.scanner import MexcScanner
from app.automation.signal_validator import ValidatedSignal, validate_signal
from app.automation.universe import ContractMeta
from app.config import Settings


def _row(timestamp_ms: int, price: float = 100.0):
    return [timestamp_ms, price, price + 1, price - 1, price, 1000]


def _valid_analysis(side: str = "LONG") -> dict:
    long = side == "LONG"
    return {
        "symbol": "BTC_USDT",
        "price": 100.0,
        "trend_4h": "BULLISH" if long else "BEARISH",
        "structure_1h": "HH/HL" if long else "LH/LL",
        "bos_15m": "BULLISH BOS" if long else "BEARISH BOS",
        "ema_direction": "BULLISH" if long else "BEARISH",
        "rsi": 60.0 if long else 40.0,
        "atr": 1.0,
        "volume": "INCREASING",
        "support": 90.0 if long else 90.0,
        "resistance": 110.0 if long else 120.0,
        "candle_time": 1_700_000_000_000,
        "score": 6,
        "setup": side,
        "bullish_points": 5 if long else 0,
        "bearish_points": 0 if long else 5,
        "entry": 100.0,
        "stop_loss": 99.0 if long else 101.0,
        "tp1": 101.5 if long else 98.5,
        "tp2": 102.5 if long else 97.5,
        "rr": 2.5,
        "reasons": [],
    }


def test_mexc_signature_is_deterministic():
    query = build_query_string({"b": "hello world", "a": "1"})
    assert query == "a=1&b=hello+world"
    assert build_signature("ACCESS", "SECRET", "123", query) == (
        "969ff47eea801bf3a7ff9d53ce51c58a79f9b163f92f2fe3370cd164c1dc43af"
    )


def test_closed_candles_drop_open_candle():
    now = 1_700_000_000_000
    rows = [
        _row(now - 1_800_000),
        _row(now - 900_000),
        _row(now),
    ]
    closed = closed_candle_rows(rows, "15m", now_ms=now)
    assert len(closed) == 2
    assert closed[-1][0] == now - 900_000


def test_confluence_rejects_conflicting_direction():
    data = _valid_analysis("LONG")
    data["trend_4h"] = "BEARISH"
    result = calculate_confluence(data)
    assert result["setup"] == "NO TRADE"
    assert result["score"] == 5


def test_trade_levels_and_position_sizing():
    long_plan = TradePlan("LONG", 100.0, 99.0, 101.5, 102.5, 2.5)
    short_plan = TradePlan("SHORT", 100.0, 101.0, 98.5, 97.5, 2.5)
    assert validate_levels(long_plan, 2.0)[0]
    assert validate_levels(short_plan, 2.0)[0]

    qty = calculate_contract_quantity(
        risk_amount_usdt=10.0,
        entry=100.0,
        stop_loss=99.0,
        contract_size=0.1,
        vol_unit=1.0,
        min_vol=1.0,
        max_vol=1000.0,
    )
    assert qty == 100.0


def test_validate_long_and_short_signals():
    long_signal, reasons = validate_signal(
        _valid_analysis("LONG"),
        min_confluence=5,
        min_rr=2.0,
        require_increasing_volume=True,
    )
    assert long_signal is not None, reasons

    short_signal, reasons = validate_signal(
        _valid_analysis("SHORT"),
        min_confluence=5,
        min_rr=2.0,
        require_increasing_volume=True,
    )
    assert short_signal is not None, reasons


class _FakeClient:
    async def get_klines(self, symbol: str, interval: str, limit: int):
        base = 1_700_000_000_000
        step = {"Hour4": 14_400_000, "Min60": 3_600_000, "Min15": 900_000, "Min5": 300_000}[interval]
        return [_row(base + i * step, 100 + i * 0.01) for i in range(80)]


class _FailingClient(_FakeClient):
    async def get_ticker(self, symbol: str):
        import time
        return {
            "ask1": "100.01",
            "bid1": "99.99",
            "lastPrice": "100.0",
            "indexPrice": "100.0",
            "fairPrice": "100.0",
            "timestamp": int(time.time() * 1000),
        }

    async def get_depth(self, symbol: str, limit: int = 10):
        return {"bids": [[99.99, 10]], "asks": [[100.01, 10]]}

    async def get_funding_rate(self, symbol: str):
        return {"fundingRate": 0.0001, "timestamp": int(__import__('time').time() * 1000)}

    async def get_deals(self, symbol: str, limit: int = 100):
        return [{"T": 1, "v": 10}, {"T": 2, "v": 8}]

    async def get_klines(self, symbol: str, interval: str, limit: int):
        if symbol == "BAD_USDT":
            raise RuntimeError("synthetic failure")
        return await super().get_klines(symbol, interval, limit)


class _FakeUniverse:
    def __init__(self):
        self.meta = ContractMeta(
            symbol="GOOD_USDT",
            quote_coin="USDT",
            settle_coin="USDT",
            contract_size=0.1,
            price_unit=0.1,
            vol_unit=1.0,
            min_vol=1.0,
            max_vol=100000.0,
            price_scale=1,
            vol_scale=0,
            state=0,
            api_allowed=True,
            hidden=False,
            future_type=1,
            pre_market=False,
        )

    async def refresh(self):
        return ["GOOD_USDT", "BAD_USDT"]

    def get(self, symbol):
        return self.meta


class _FakeSignalManager:
    async def publish(self, signal):
        return True


class _FakeExecutor:
    async def execute(self, signal, meta):
        return ExecutionResult(False, None, "disabled")


def test_scanner_isolates_one_bad_symbol(monkeypatch):
    import app.automation.scanner as scanner_module

    monkeypatch.setattr(scanner_module, "analyze_candles", lambda *args, **kwargs: _valid_analysis("LONG"))
    settings = Settings(
        scanner_enabled=True,
        auto_signal_enabled=True,
        auto_trade_enabled=False,
        scan_concurrency=2,
        candle_limit=80,
        min_confluence=5,
        min_rr=2.0,
    )
    scanner = MexcScanner(
        client=_FailingClient(),
        settings=settings,
        universe=_FakeUniverse(),
        signal_manager=_FakeSignalManager(),
        executor=_FakeExecutor(),
    )
    result = asyncio.run(scanner.scan_once())
    assert result["symbols"] == 2
    assert result["valid"] == 1
    assert result["sent"] == 1
    assert result["errors"] == 1


def test_mexc_kline_parser():
    settings = Settings()
    client = MexcClient(settings)

    calls = {}

    async def fake_request(*args, **kwargs):
        calls["params"] = kwargs["params"]
        return {
            "time": [1_700_000_000],
            "open": [100],
            "high": [101],
            "low": [99],
            "close": [100.5],
            "vol": [1234],
        }

    async def run():
        client._request = fake_request  # type: ignore[method-assign]
        rows = await client.get_klines("BTC_USDT", "Min15", 200)
        await client.close()
        return rows

    rows = asyncio.run(run())
    assert rows == [[1_700_000_000_000, 100.0, 101.0, 99.0, 100.5, 1234.0]]
    assert calls["params"]["interval"] == "Min15"
    assert calls["params"]["end"] > calls["params"]["start"]


def test_executor_gate_stays_closed_even_when_requested(monkeypatch):
    settings = Settings(auto_trade_enabled=True, allow_live_execution=True)
    client = object()
    executor = MexcExecutor(client, settings)
    signal, reasons = validate_signal(
        _valid_analysis("LONG"),
        min_confluence=5,
        min_rr=2.0,
        require_increasing_volume=False,
    )
    assert signal is not None, reasons

    meta = _FakeUniverse().meta
    result = asyncio.run(executor.execute(signal, meta))
    assert result.executed is False
    assert "disabled" in result.message.lower()


def test_order_payload_side_mapping():
    meta = _FakeUniverse().meta
    long_signal, reasons = validate_signal(
        _valid_analysis("LONG"),
        min_confluence=5,
        min_rr=2.0,
        require_increasing_volume=False,
    )
    assert long_signal is not None, reasons
    payload = build_limit_order_payload(
        long_signal, meta, risk_amount_usdt=10.0, leverage=3, open_type=1
    )
    assert payload["side"] == 1
    assert payload["type"] == 1
    assert payload["openType"] == 1
    assert payload["vol"] == 100.0


def test_signal_database_deduplication(tmp_path):
    from app.database import Database

    db = Database(str(tmp_path / "signals.sqlite3"))
    kwargs = dict(
        signal_key="same-signal",
        symbol="BTC_USDT",
        side="LONG",
        candle_time=1700000000000,
        entry=100.0,
        stop_loss=99.0,
        tp1=101.5,
        tp2=102.5,
        rr=2.5,
        confluence=6,
        analysis_json="{}",
        created_at="2026-09-24T00:00:00+00:00",
        expires_at="2026-09-24T00:30:00+00:00",
    )
    assert db.create_signal_if_new(**kwargs) is True
    assert db.create_signal_if_new(**kwargs) is False
    assert db.get_signal("same-signal").status == "NEW"

class _MarketFakeClient:
    async def get_contracts(self):
        return [{
            "symbol": "BTC_USDT",
            "state": 0,
            "apiAllowed": True,
            "isHidden": False,
            "preMarket": False,
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "futureType": 1,
            "baseCoin": "BTC",
        }]

    async def get_ticker(self, symbol):
        assert symbol == "BTC_USDT"
        return {"lastPrice": "100.0", "bid1": "99.9", "ask1": "100.1", "indexPrice": "100.0", "fairPrice": "100.0"}

    async def get_klines(self, symbol, interval, limit):
        assert symbol == "BTC_USDT"
        assert interval == "Min15"
        return [_row(1_700_000_000_000 + i * 900_000, 100 + i) for i in range(20)]

    async def close(self):
        pass


def test_mexc_market_adapter_is_mexc_only():
    from app.market import MarketData
    market = MarketData(Settings(), client=_MarketFakeClient())
    ref = asyncio.run(market.resolve("BTCUSDT"))
    assert ref == __import__("app.market", fromlist=["MarketRef"]).MarketRef("mexc", "BTC_USDT")
    assert asyncio.run(market.price("BTC_USDT")) == 100.0
    assert asyncio.run(market.executable_price(ref, "LONG")) == 100.1
    assert asyncio.run(market.executable_price(ref, "SHORT")) == 99.9
    rows = asyncio.run(market.ohlcv(ref, "15M", 20))
    assert len(rows) == 20


def test_mexc_phase2_settings_defaults():
    from app.config import Settings

    settings = Settings()
    assert settings.max_mexc_spread_pct > 0
    assert settings.max_index_dislocation_pct > 0
    assert settings.max_data_age_seconds > 0
    assert settings.orderbook_levels >= 1
    assert settings.trade_flow_limit >= 1


def test_contract_meta_contains_fee_rates():
    from app.automation.universe import ContractMeta

    meta = ContractMeta(
        symbol="BTC_USDT",
        quote_coin="USDT",
        settle_coin="USDT",
        contract_size=1.0,
        price_unit=0.1,
        vol_unit=1.0,
        min_vol=1.0,
        max_vol=1000.0,
        price_scale=1,
        vol_scale=0,
        state=0,
        api_allowed=True,
        hidden=False,
        future_type=1,
        pre_market=False,
        maker_fee_rate=0.0006,
        taker_fee_rate=0.0008,
    )
    assert meta.maker_fee_rate == 0.0006
    assert meta.taker_fee_rate == 0.0008
