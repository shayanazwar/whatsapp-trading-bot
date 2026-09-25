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
    return {"symbol":"BTC_USDT","price":100.0,"trend_4h":"BULLISH" if long else "BEARISH","structure_1h":"HH/HL" if long else "LH/LL","bos_15m":True,"ema_direction":"BULLISH" if long else "BEARISH","rsi":60.0 if long else 40.0,"rsi_5m":60.0 if long else 40.0,"atr":1.0,"atr_percentile":50,"volume":"INCREASING","rvol":1.5,"rvol_15m":1.5,"rvol_5m":1.5,"entry":100.0,"stop_loss":99.0 if long else 101.0,"tp1":101.5 if long else 98.5,"tp2":102.5 if long else 97.5,"rr":2.5,"setup":side,"setup_bos_time":fifteen-300000,"setup_retest_time":fifteen,"closed_5m_candle_time":five,"candle_time":fifteen,"direction_ok":True,"structure_ok":True,"setup_ok":True,"momentum_ok":True,"volume_ok":True,"location_ok":True,"futures_ok":True,"btc_filter_ok":True,"volatility_ok":True,"data_fresh":True,"target_path_structural":True,"five_minute_ready":True,"five_minute_long":long,"five_minute_short":not long,"confirmation_family_count":6,"trigger_quality_5m":0.8,"score":100,"sl_atr":1.0,"signal_blocked":False,"technical_candidate":True,"mexc_spread_pct":0.0001,"max_mexc_spread_pct":0.001,"entry_drift_pct":0.0,"max_entry_drift_pct":0.002,"max_signal_age_seconds":330}


def test_signature_is_deterministic():
    q=build_query_string({"b":"hello world","a":"1"}); assert q=="a=1&b=hello+world"; assert build_signature("ACCESS","SECRET","123",q)=="969ff47eea801bf3a7ff9d53ce51c58a79f9b163f92f2fe3370cd164c1dc43af"


def test_closed_candle_normalization_and_legacy_index_access():
    now=1_700_000_000_000; rows=[_row(now-1_800_000),_row(now-900_000),_row(now)]; closed=closed_candle_rows(rows,"15m",now_ms=now); assert len(closed)==2; assert closed[-1][0]==now-900_000; assert closed[-1]["time"]==now-900_000


def test_legacy_confluence_compatibility():
    d=_valid_analysis("LONG"); d["trend_4h"]="BEARISH"; out=calculate_confluence(d); assert out["setup"]=="NO TRADE" and out["score"]==5


def test_trade_levels_and_position_sizing():
    long_plan=TradePlan("LONG",100,99,101.5,102.5,2.5); assert validate_levels(long_plan,2)[0]; qty=calculate_contract_quantity(10,100,99,0.1,1,1,1000); assert qty==99.0
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


def test_signal_format_uses_score_100():
    signal, reasons = validate_signal(_valid_analysis("LONG"), min_confluence=82, min_rr=2, require_increasing_volume=False)
    assert signal is not None, reasons
    text = format_signal(signal)
    assert "Score: 100/100" in text
    assert "Families: 6/6" in text
