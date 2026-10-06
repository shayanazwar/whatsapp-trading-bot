from __future__ import annotations

import asyncio
import time

import app.automation.scanner as scanner_module
from app.analysis.engine import closed_candle_rows, synthesize_12h_from_4h
from app.automation.executor import MexcExecutor, build_limit_order_payload
from app.automation.mexc_client import MexcClient, build_query_string, build_signature
from app.automation.risk_manager import TradePlan, calculate_contract_quantity, calculate_risk_amount, validate_levels
from app.automation.scanner import MexcScanner
from app.automation.setup_filter import validate_analysis
from app.automation.signal_manager import format_signal
from app.automation.signal_validator import validate_signal
from app.automation.universe import ContractMeta
from app.config import Settings


def _row(ts: int, price: float = 100.0):
    return [ts, price, price + 1, price - 1, price, 1000]


def _valid_analysis(side: str = "LONG", *, candle_time: int | None = None) -> dict:
    now = int(time.time() * 1000)
    signal_time = candle_time if candle_time is not None else now
    return {
        "symbol": "BTC_USDT", "price": 100.0, "setup": side,
        "setup_bos_time": signal_time - 7_200_000,
        "long_bos_level": 100.0, "short_bos_level": 100.0,
        "score": 80, "direction_ok": True, "structure_ok": True,
        "setup_ok": True, "confirmation_ok": True, "location_ok": True,
        "target_path_structural": True, "structure_quality_ok": True,
        "shock_veto_ok": True, "technical_candidate": True,
        "trade_geometry_ok": True, "risk_ok": True,
        "primary_entry_timeframe": "1H", "signal_candle_timeframe": "1H",
        "entry": 100.0, "stop_loss": 99.0 if side == "LONG" else 101.0,
        "tp": 104.0 if side == "LONG" else 96.0, "rr": 3.48, "atr": 1.0,
        "sl_atr": 1.0, "tp_distance_atr": 4.0, "mexc_spread_pct": 0.01,
        "max_allowed_spread_pct": 0.50, "max_signal_age_seconds": 5400,
        "candle_time": signal_time, "mexc_funding_rate": 0.0,
    }


def test_signature_is_deterministic():
    query = build_query_string({"b": "hello world", "a": "1"})
    assert query == "a=1&b=hello+world"
    assert build_signature("ACCESS", "SECRET", "123", query) == "969ff47eea801bf3a7ff9d53ce51c58a79f9b163f92f2fe3370cd164c1dc43af"


def test_closed_candle_normalization_uses_only_approved_signal_timeframes():
    now = 1_700_000_000_000
    rows = [_row(now - 7_200_000), _row(now - 3_600_000), _row(now)]
    closed = closed_candle_rows(rows, "1H", now_ms=now)
    assert len(closed) == 2
    assert closed[-1]["time"] == now - 3_600_000


def test_12h_aggregation_is_causal():
    base = (1_700_000_000_000 // 43_200_000) * 43_200_000
    rows = [_row(base + i * 14_400_000, 100 + i) for i in range(4)]
    out = synthesize_12h_from_4h(rows, now_ms=base + 43_200_000)
    assert len(out) == 1
    assert out[0]["open"] == 100
    assert out[0]["close"] == 102


def test_validate_signal_accepts_1h_structural_signal():
    signal, reasons = validate_signal(_valid_analysis(), min_confluence=65, min_rr=2.0)
    assert signal is not None, reasons
    assert signal.analysis["primary_entry_timeframe"] == "1H"


def test_validate_analysis_rejects_score_below_new_supporting_evidence_floor():
    data = _valid_analysis()
    data["score"] = 64
    ok, reasons = validate_analysis(data, min_confluence=65, min_rr=2.0)
    assert not ok
    assert any("Score 64 < required 65" in reason for reason in reasons)


def test_executor_live_switch_remains_closed():
    settings = Settings(auto_trade_enabled=True, allow_live_execution=True)
    executor = MexcExecutor(object(), settings)
    signal, reasons = validate_signal(_valid_analysis(), min_confluence=65, min_rr=2.0)
    assert signal is not None, reasons
    meta = ContractMeta("BTC_USDT", "USDT", "USDT", 0.1, 0.1, 1, 1, 1000, 1, 0, 0, True, False, 1, False)
    result = asyncio.run(executor.execute(signal, meta))
    assert result.executed is False
    assert "disabled" in result.message.lower()


def test_order_payload_side_mapping():
    meta = ContractMeta("BTC_USDT", "USDT", "USDT", 0.1, 0.1, 1, 1, 1000, 1, 0, 0, True, False, 1, False)
    signal, reasons = validate_signal(_valid_analysis(), min_confluence=65, min_rr=2.0)
    assert signal is not None, reasons
    payload = build_limit_order_payload(signal, meta, risk_amount_usdt=10, leverage=3, open_type=1)
    assert payload["side"] == 1
    assert payload["openType"] == 1


def test_public_request_rate_limit_retries_with_shared_backoff():
    settings = Settings(
        mexc_public_min_interval_seconds=0,
        mexc_public_window_seconds=10,
        mexc_public_window_limit=100,
        mexc_rate_limit_max_retries=2,
        mexc_rate_limit_backoff_seconds=0.01,
        mexc_rate_limit_backoff_cap_seconds=0.02,
        mexc_rate_limit_jitter_seconds=0,
    )
    client = MexcClient(settings)

    class FakeResponse:
        status_code = 200
        is_error = False
        text = ""
        headers = {}
        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload

    responses = [FakeResponse({"success": False, "message": "Requests are too frequent", "code": "429"}), FakeResponse({"success": True, "data": {"ok": True}})]
    calls = []

    async def fake_request(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    async def run():
        client.http.request = fake_request
        return await client._public_request({"method": "GET", "url": "https://example.test"})

    response = asyncio.run(run())
    assert response.status_code == 200
    assert len(calls) == 2
    assert client._public_retry_events == 1
    asyncio.run(client.close())


def test_live_scanner_uses_1h_close_time_not_1h_open_time_for_freshness(monkeypatch):
    fixed_now = int(time.time() * 1000)
    monkeypatch.setattr(scanner_module.time, "time", lambda: fixed_now / 1000)
    from app.automation import signal_validator as validator_module
    monkeypatch.setattr(validator_module.time, "time", lambda: fixed_now / 1000)

    step = 3_600_000
    base_4h = (fixed_now // (4 * step)) * (4 * step) - 200 * (4 * step)
    rows_4h = [_row(base_4h + i * 4 * step) for i in range(200)]
    rows_1h = [_row(fixed_now - 180 * step + i * step) for i in range(180)]
    rows_1d = [_row((fixed_now // (86_400_000)) * 86_400_000 - 180 * 86_400_000 + i * 86_400_000) for i in range(180)]
    fake_analysis = _valid_analysis(candle_time=fixed_now - step)
    fake_analysis["candle_close_time"] = fixed_now

    async def fake_get_closed(_symbol, timeframe, _limit):
        return {"4H": rows_4h, "1H": rows_1h, "1D": rows_1d}[timeframe], False

    class FakeClient:
        async def get_ticker(self, symbol):
            return {"lastPrice": 100.0, "bid1": 99.99, "ask1": 100.01, "timestamp": fixed_now}

    class FakeUniverse:
        async def refresh(self):
            return ["BTC_USDT"]

    class FakeSignalManager:
        async def publish(self, signal):
            return True

    monkeypatch.setattr(scanner_module, "analyze_candles", lambda *args, **kwargs: dict(fake_analysis))
    scanner = MexcScanner(settings=Settings(max_signal_age_seconds=1200, min_confluence=65, min_rr=2.0), client=FakeClient(), universe=FakeUniverse(), signal_manager=FakeSignalManager())
    monkeypatch.setattr(scanner, "_get_closed_candles", fake_get_closed)
    result = asyncio.run(scanner._scan_one("BTC_USDT"))
    assert result["sent"] is True
    assert result["analysis"]["candle_time"] == fixed_now


def test_scanner_reports_distinct_stage_reasons_instead_of_generic_no_setup(monkeypatch):
    fixed_now = int(time.time() * 1000)
    step = 3_600_000
    base_4h = (fixed_now // (4 * step)) * (4 * step) - 200 * (4 * step)
    rows_4h = [_row(base_4h + i * 4 * step) for i in range(200)]
    rows_1h = [_row(fixed_now - 180 * step + i * step) for i in range(180)]
    rows_1d = [_row((fixed_now // (86_400_000)) * 86_400_000 - 180 * 86_400_000 + i * 86_400_000) for i in range(180)]
    analysis = _valid_analysis(candle_time=fixed_now)
    analysis.update({"setup": "NO TRADE", "diagnostic_failures": ["4H BOS/retest structure"], "rejection_stage": "4H_SETUP"})

    class FakeClient:
        async def get_ticker(self, symbol): return {"lastPrice": 100.0}
    class FakeUniverse:
        async def refresh(self): return ["X_USDT"]
    class FakeSignalManager: pass

    monkeypatch.setattr(scanner_module, "analyze_candles", lambda *args, **kwargs: analysis)
    scanner = MexcScanner(settings=Settings(), client=FakeClient(), universe=FakeUniverse(), signal_manager=FakeSignalManager())
    async def fake_get_closed(_symbol, _tf, _limit):
        return {"4H": rows_4h, "1H": rows_1h, "1D": rows_1d}[_tf], False
    monkeypatch.setattr(scanner, "_get_closed_candles", fake_get_closed)
    result = asyncio.run(scanner._scan_one("X_USDT"))
    assert result["rejection_stage"] == "4H_SETUP"
    assert result["rejection_reasons"] == ["4H BOS/retest structure"]


def test_signal_format_uses_1d_12h_4h_1h_basis():
    signal, reasons = validate_signal(_valid_analysis(), min_confluence=65, min_rr=2.0)
    assert signal is not None, reasons
    text = format_signal(signal)
    assert "1D:" in text and "12H:" in text and "4H:" in text and "1H:" in text
    assert "15M" not in text


def test_risk_math_is_consistent():
    plan = TradePlan("LONG", 100, 95, 110, 3.0)
    assert validate_levels(plan, 2.0) == (True, "OK")
    assert calculate_risk_amount(1000, 1.0) == 10.0
    assert calculate_contract_quantity(10, 100, 95, 1, 0.1, 0.1, 10, cost_buffer_pct=0) == 2.0
