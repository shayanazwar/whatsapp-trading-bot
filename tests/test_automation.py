from __future__ import annotations

import asyncio
import time

from app.analysis.engine import calculate_confluence, closed_candle_rows
from app.automation.executor import ExecutionResult, MexcExecutor, build_limit_order_payload
from app.automation.mexc_client import MexcClient, build_query_string, build_signature
from app.automation.risk_manager import TradePlan, calculate_contract_quantity, calculate_risk_amount, validate_levels
from app.automation.scanner import MexcScanner
from app.automation.signal_manager import format_signal, SignalManager
from app.automation.signal_validator import ValidatedSignal, validate_signal
from app.automation.universe import ContractMeta
from app.config import Settings


def _row(ts: int, price: float = 100.0): return [ts, price, price + 1, price - 1, price, 1000]


def _valid_analysis(side="LONG"):
    long = side == "LONG"; now = int(time.time() * 1000); fifteen = now - (now % 900000); five = now - (now % 300000)
    families = {
        "momentum":{"status":"PASS","value":0.8},
        "relative_volume":{"status":"PASS","value":1.5},
        "volatility_regime":{"status":"PASS","value":50},
        "liquidity_quality":{"status":"ABSTAIN"},
        "funding_crowding":{"status":"ABSTAIN"},
        "flow_pressure":{"status":"PASS","value":0.2 if long else -0.2},
        "htf_target_path":{"status":"PASS","value":3.0},
        "vwap_location":{"status":"PASS","value":0.5},
    }
    return {"symbol":"BTC_USDT","price":100.0,"trend_4h":"BULLISH" if long else "BEARISH","structure_1h":"HH/HL" if long else "LH/LL","bos_15m":True,"ema_direction":"BULLISH" if long else "BEARISH","rsi":60.0 if long else 40.0,"rsi_5m":60.0 if long else 40.0,"atr":1.0,"atr_percentile":50,"volume":"INCREASING","rvol":1.5,"rvol_15m":1.5,"rvol_5m":1.5,"entry":100.0,"stop_loss":99.0 if long else 101.0,"tp":104.0 if long else 96.0,"rr":3.48,"setup":side,"setup_bos_time":fifteen-300000,"setup_retest_time":fifteen,"candle_time":fifteen,"direction_ok":True,"structure_ok":True,"setup_ok":True,"momentum_ok":True,"volume_ok":True,"location_ok":True,"futures_ok":True,"btc_filter_ok":True,"volatility_ok":True,"data_fresh":True,"target_path_structural":True,"shock_veto_ok":True,"confirmation_families":families,"confirmation_families_passed":6,"confirmation_families_available":6,"confirmation_family_diversity_ok":True,"confirmation_family_count":6,"supporting_family_count":6,"score":100,"sl_atr":1.0,"tp_distance_atr":4.0,"signal_blocked":False,"technical_candidate":True,"structure_quality_ok":True,"ema_extension_ok":True,"five_minute_ready":False,"five_minute_long":False,"five_minute_short":False,"mexc_spread_pct":0.0001,"max_mexc_spread_pct":0.001,"entry_drift_pct":0.0,"max_entry_drift_pct":0.002,"max_signal_age_seconds":1200,"estimated_round_trip_cost_pct":0.0015}


def test_signature_is_deterministic():
    q=build_query_string({"b":"hello world","a":"1"}); assert q=="a=1&b=hello+world"; assert build_signature("ACCESS","SECRET","123",q)=="969ff47eea801bf3a7ff9d53ce51c58a79f9b163f92f2fe3370cd164c1dc43af"


def test_closed_candle_normalization_and_legacy_index_access():
    now=1_700_000_000_000; rows=[_row(now-1_800_000),_row(now-900_000),_row(now)]; closed=closed_candle_rows(rows,"15m",now_ms=now); assert len(closed)==2; assert closed[-1][0]==now-900_000; assert closed[-1]["time"]==now-900_000


def test_legacy_confluence_compatibility():
    d=_valid_analysis("LONG"); d["trend_4h"]="BEARISH"; out=calculate_confluence(d); assert out["setup"]=="NO TRADE" and out["score"]==5


def test_trade_levels_and_position_sizing():
    long_plan=TradePlan("LONG",100,99,104,2.5); assert validate_levels(long_plan,2)[0]; qty=calculate_contract_quantity(10,100,99,0.1,1,1,1000); assert qty==99.0
    assert round(calculate_risk_amount(1000,1.0),2)==10.0


def test_validate_signal():
    signal,reasons=validate_signal(_valid_analysis("LONG"),min_confluence=82,min_rr=2,require_increasing_volume=True); assert signal is not None,reasons


def test_executor_gate_stays_closed():
    settings=Settings(auto_trade_enabled=True,allow_live_execution=True); executor=MexcExecutor(object(),settings); signal,_=validate_signal(_valid_analysis("LONG"),min_confluence=82,min_rr=2,require_increasing_volume=False); assert signal is not None
    meta=ContractMeta("BTC_USDT","USDT","USDT",0.1,0.1,1,1,1000,1,0,0,True,False,1,False)
    result=asyncio.run(executor.execute(signal,meta)); assert result.executed is False and "disabled" in result.message.lower()


def test_order_payload_side_mapping():
    meta=ContractMeta("BTC_USDT","USDT","USDT",0.1,0.1,1,1,1000,1,0,0,True,False,1,False)
    signal,_=validate_signal(_valid_analysis("LONG"),min_confluence=82,min_rr=2,require_increasing_volume=False); assert signal
    p=build_limit_order_payload(signal,meta,risk_amount_usdt=10,leverage=3,open_type=1); assert p["side"]==1; assert p["openType"]==1


def test_client_kline_parser():
    c=MexcClient(Settings()); calls={}
    async def fake(*args,**kwargs): calls.update(kwargs["params"]); return {"time":[1700000000],"open":[100],"high":[101],"low":[99],"close":[100.5],"vol":[1234]}
    async def run(): c._request=fake; rows=await c.get_klines("BTC_USDT","Min15",200); await c.close(); return rows
    rows=asyncio.run(run()); assert rows==[[1700000000000,100.0,101.0,99.0,100.5,1234.0]]; assert calls["interval"]=="Min15"


def test_mexc_trade_flow_uses_official_T_v_fields():
    flow = MexcScanner._calculate_trade_flow([
        {"T": 1, "v": 10},
        {"T": 2, "v": 4},
    ])
    assert flow["buy_volume"] == 10.0
    assert flow["sell_volume"] == 4.0
    assert flow["volume_delta"] == 6.0


def test_signal_validation_does_not_require_5m_or_increasing_volume():
    data = _valid_analysis("LONG")
    data.pop("five_minute_ready", None)
    data.pop("five_minute_long", None)
    data.pop("five_minute_short", None)
    data.pop("trigger_quality_5m", None)
    data["primary_entry_timeframe"] = "15M"
    data["volume"] = "DECREASING"
    data["confirmation_family_count"] = 5
    data["confirmation_families_passed"] = 5
    data["confirmation_families_available"] = 6
    data["momentum_quality"] = 0.60
    data["volume_quality"] = 0.30
    signal, reasons = validate_signal(data, min_confluence=82, min_rr=2.0, require_increasing_volume=True)
    assert signal is not None, reasons


def test_signal_format_uses_score_100():
    signal, reasons = validate_signal(_valid_analysis("LONG"), min_confluence=82, min_rr=2, require_increasing_volume=False)
    assert signal is not None, reasons
    text = format_signal(signal)
    assert "Score: 100/100" in text
    assert "Families: 6/6" in text


class _FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload or "")
        self.headers = _FakeHeaders(headers or {})
        self.is_error = status_code >= 400

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


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
    responses = [
        _FakeResponse(200, {"success": False, "message": "Requests are too frequent", "code": "429"}),
        _FakeResponse(200, {"success": True, "data": {"ok": True}}),
    ]
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
    assert client._public_rate_limit_events == 1
    assert client._public_retry_events == 1
    asyncio.run(client.close())


def test_public_request_retries_http_429_and_honors_bounded_retry_count():
    settings = Settings(
        mexc_public_min_interval_seconds=0,
        mexc_public_window_seconds=10,
        mexc_public_window_limit=100,
        mexc_rate_limit_max_retries=1,
        mexc_rate_limit_backoff_seconds=0.01,
        mexc_rate_limit_backoff_cap_seconds=0.02,
        mexc_rate_limit_jitter_seconds=0,
    )
    client = MexcClient(settings)
    responses = [
        _FakeResponse(429, {"message": "too many requests"}, headers={"Retry-After": "0"}),
        _FakeResponse(200, {"success": True, "data": {"ok": True}}),
    ]

    async def fake_request(**kwargs):
        return responses.pop(0)

    async def run():
        client.http.request = fake_request
        return await client._public_request({"method": "GET", "url": "https://example.test"})

    response = asyncio.run(run())
    assert response.status_code == 200
    assert client._public_rate_limit_events == 1
    asyncio.run(client.close())


def test_live_scanner_stages_5m_and_1d_after_technical_prefilter(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def get_klines(self, symbol, interval, limit):
            self.calls.append((symbol, interval))
            step = {"Hour4": 14_400_000, "Min60": 3_600_000, "Min15": 900_000, "Min5": 300_000, "Day1": 86_400_000}[interval]
            end = int(time.time() * 1000)
            base = end - 259 * step
            return [[base + i * step, 100, 101, 99, 100, 1000] for i in range(260)]

    class FakeUniverse:
        async def refresh(self):
            return ["X_USDT"]

    class FakeSignals:
        pass

    old_helpers = (
        __import__("app.automation.scanner", fromlist=["_four_hour_regime"])._four_hour_regime,
        __import__("app.automation.scanner", fromlist=["_one_hour_alignment"])._one_hour_alignment,
        __import__("app.automation.scanner", fromlist=["_bos_events"])._bos_events,
        __import__("app.automation.scanner", fromlist=["_select_latest_bos_with_retest"])._select_latest_bos_with_retest,
        __import__("app.automation.scanner", fromlist=["_fifteen_minute_entry_confirmation"])._fifteen_minute_entry_confirmation,
        __import__("app.automation.scanner", fromlist=["analyze_candles"]).analyze_candles,
    )
    import app.automation.scanner as scanner_module

    def install(force_pass):
        monkeypatch.setattr(scanner_module, "_four_hour_regime", lambda c: {"bull": force_pass, "bear": False, "regime": "BULLISH" if force_pass else "NO_TRADE"})
        monkeypatch.setattr(scanner_module, "_one_hour_alignment", lambda c, r: {"long": force_pass, "short": False, "long_votes": 4 if force_pass else 0, "short_votes": 0})
        monkeypatch.setattr(scanner_module, "_bos_events", lambda c, side: [{"level": 100, "time": c[-2]["time"], "strength": 1}] if side == "LONG" else [])
        monkeypatch.setattr(scanner_module, "_select_latest_bos_with_retest", lambda c, side, events: ({"level": 100, "strength": 1}, {"valid": force_pass, "time": c[-2]["time"], "quality": 1.0, "rejection": True}) if side == "LONG" else (None, {"valid": False}))
        monkeypatch.setattr(scanner_module, "_fifteen_minute_entry_confirmation", lambda *args: {"ready": force_pass, "quality": 1})
        monkeypatch.setattr(scanner_module, "analyze_candles", lambda *args, **kwargs: {"setup": "NO TRADE"})

    async def run(force_pass):
        client = FakeClient()
        install(force_pass)
        scanner = MexcScanner(settings=Settings(scan_concurrency=4), client=client, universe=FakeUniverse(), signal_manager=FakeSignals())
        await scanner.scan_once()
        return [interval for symbol, interval in client.calls if symbol == "X_USDT"]

    rejected = asyncio.run(run(False))
    passed = asyncio.run(run(True))
    assert rejected == ["Hour4", "Min60", "Min15"]
    assert passed == ["Hour4", "Min60", "Min15", "Min5", "Day1"]
