from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import httpx
import pytest

from app.backtesting import engine as backtest_engine
from app.market.candles import validate_candle_continuity
from app.market.mexc import MexcAPIError, MexcPublicClient
from app.risk.manager import size_position
from app.risk.market_validation import refresh_trade_levels
from app.strategy.indicators import rsi


def _frame(start: datetime, count: int, seconds: int, *, base: float = 100.0) -> pd.DataFrame:
    rows = []
    for index in range(count):
        price = base
        rows.append({
            "timestamp": start + timedelta(seconds=index * seconds),
            "open": price, "high": price + 1, "low": price - 1,
            "close": price, "volume": 100.0, "amount": 10000.0,
        })
    return pd.DataFrame(rows)


def _http_response(data):
    response = Mock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {"success": True, "data": data}
    response.raise_for_status.return_value = None
    return response


def test_candle_continuity_rejects_missing_hour():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    frame = _frame(start, 4, 3600).drop(index=2)
    with pytest.raises(ValueError, match="not contiguous"):
        validate_candle_continuity(frame, 3600)


def test_mexc_empty_kline_page_advances_cursor_instead_of_hanging():
    start = 1767225600  # 2026-01-01 00:00 UTC
    second_page_start = start + 1900 * 3600
    empty = {key: [] for key in ("time", "open", "high", "low", "close", "vol", "amount")}
    one = {
        "time": [second_page_start], "open": [100], "high": [102], "low": [99],
        "close": [101], "vol": [10], "amount": [1000],
    }
    http = Mock()
    http.get.side_effect = [_http_response(empty), _http_response(one)]
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0
    frame = client.klines("BTC_USDT", "Min60", start, second_page_start + 3600)
    assert http.get.call_count == 2
    assert len(frame) == 1
    assert frame.iloc[0].timestamp.timestamp() == second_page_start


def test_mexc_rejects_unequal_kline_arrays():
    body = {
        "time": [1767225600], "open": [100, 100], "high": [102], "low": [99],
        "close": [101], "vol": [10], "amount": [1000],
    }
    http = Mock()
    http.get.return_value = _http_response(body)
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0
    with pytest.raises(MexcAPIError, match="unequal array lengths"):
        client.klines("BTC_USDT", "Min60", 1767225600, 1767229200)


def test_position_size_obeys_margin_including_opening_costs():
    plan = size_position(
        equity=1000, risk_pct=5, entry=100, stop=99.99, side="LONG",
        contract_size=1, vol_unit=1, leverage=2, taker_fee_rate=0.001,
        slippage_bps=0, initial_margin_rate=0.5, available_margin_usdt=1000,
        margin_buffer_pct=10, min_contracts=1, max_contracts=100, max_leverage=5,
    )
    open_cost = plan.notional_usdt * (0.001)
    assert plan.initial_margin_estimate_usdt + open_cost <= 900 + 1e-8
    assert plan.contracts <= 17


def test_position_size_rejects_leverage_above_contract_max():
    with pytest.raises(ValueError, match="exceeds contract maximum"):
        size_position(
            equity=1000, risk_pct=1, entry=100, stop=95, side="LONG",
            contract_size=1, vol_unit=1, leverage=10, taker_fee_rate=0.001,
            max_leverage=5,
        )


def test_fresh_quote_rejects_when_original_tp2_already_reached():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    ticker = {
        "timestamp": int(now.timestamp() * 1000),
        "bid1": 120.0, "ask1": 120.1,
    }
    with pytest.raises(ValueError, match="Original TP2 has already been reached"):
        refresh_trade_levels(
            ticker=ticker, contract={"priceUnit": 0.1}, side="LONG",
            reference_entry=100, stop=90, reference_tp2=120, atr=20,
            max_entry_drift_atr=2, now=now,
        )


def test_fresh_quote_rejects_excessive_spread():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    ticker = {"timestamp": int(now.timestamp() * 1000), "bid1": 99.0, "ask1": 101.0}
    with pytest.raises(ValueError, match="Spread"):
        refresh_trade_levels(
            ticker=ticker, contract={"priceUnit": 0.1}, side="LONG",
            reference_entry=100, stop=90, atr=10, max_spread_bps=10, now=now,
        )


def test_rsi_monotonic_up_down_and_flat_series():
    up = pd.Series(np.arange(1.0, 40.0))
    down = pd.Series(np.arange(40.0, 1.0, -1.0))
    flat = pd.Series(np.full(40, 10.0))
    assert rsi(up).iloc[-1] == pytest.approx(100.0)
    assert rsi(down).iloc[-1] == pytest.approx(0.0)
    assert rsi(flat).iloc[-1] == pytest.approx(50.0)


def test_partial_tp1_then_gap_stop_accounts_both_legs(monkeypatch):
    # All higher-timeframe candles are completed, contiguous and UTC aligned.
    d1 = _frame(datetime(2025, 1, 1, tzinfo=timezone.utc), 210, 86400)
    h4 = _frame(datetime(2025, 1, 1, tzinfo=timezone.utc), 600, 14400)
    h1 = _frame(datetime(2026, 2, 1, tzinfo=timezone.utc), 58, 3600)
    # Entry next candle opens at 100; next bar reaches TP1; following bar gaps
    # below the 90 stop and must fill at the adverse open (80), not at stop 90.
    h1.loc[56, ["high", "low", "close"]] = [111.0, 99.0, 108.0]
    h1.loc[57, ["open", "high", "low", "close"]] = [80.0, 82.0, 79.0, 80.0]

    candidate = SimpleNamespace(
        decision="LONG_CANDIDATE", side="LONG", stop=90.0, setup_4h="TEST_SETUP", score=80.0,
    )

    def fake_strategy(symbol, d1_visible, h12_visible, h4_visible, h1_visible, **kwargs):
        if len(h1_visible) >= 55:
            return candidate
        return SimpleNamespace(decision="WAIT", side=None, stop=None, setup_4h=None, score=0)

    monkeypatch.setattr(backtest_engine, "analyze_multitimeframe", fake_strategy)
    contract = {
        "takerFeeRate": 0.0, "feeRateMode": "NORMAL", "feeRateType": "BASE",
        "contractSize": 1.0, "volUnit": 1.0, "priceUnit": 0.01,
        "minVol": 1.0, "maxVol": 100.0, "limitMaxVol": 100.0,
        "maxLeverage": 5, "initialMarginRate": 0.2, "maintenanceMarginRate": 0.005,
        "riskLimitMode": "NORMAL",
    }
    start_time = h1.iloc[54]["timestamp"] + pd.Timedelta(hours=1)
    end_time = h1.iloc[-1]["timestamp"] + pd.Timedelta(hours=1)
    filter_calls = []
    def approve_kronos(prefix, side, plan):
        filter_calls.append({"side": side, **plan})
        return {"approved": True, "forecast_tp_sl_race": {"mean_r_after_fees_slippage_before_funding": 0.25}}

    result = backtest_engine.run_event_backtest(
        symbol="BTC_USDT", candles_1h=h1, candles_4h=h4, candles_1d=d1,
        contract=contract, start_time=start_time, end_time=end_time,
        starting_equity=10_000, risk_pct=0.5, slippage_bps=0, min_net_rr=1.5,
        use_kronos_filter=True, kronos_filter=approve_kronos,
        funding_events=[], leverage=2, margin_buffer_pct=10,
    )
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["tp1_touched"] is True
    assert trade["exit_type"] == "STOP_AFTER_TP1"
    assert trade["exit_price"] == 80.0
    # Each contract: +5 from half at TP1 and -10 from half at the gap fill = -5.
    assert trade["gross_pnl_usdt"] == pytest.approx(-25.0)
    assert trade["net_pnl_usdt"] == pytest.approx(-25.0)
    assert trade["kronos_filter_result"]["forecast_tp_sl_race"]["mean_r_after_fees_slippage_before_funding"] == pytest.approx(0.25)
    assert len(filter_calls) == 1
    assert filter_calls[0]["entry"] == 100.0
    assert filter_calls[0]["stop"] == 90.0
    assert filter_calls[0]["tp1"] == 110.0
    assert filter_calls[0]["tp2"] == 125.0
    assert len(result["input_data_sha256"]) == 64
    # Drawdown must include mark-to-market and the final realized loss.
    assert result["metrics"]["max_drawdown_pct"] > 0
    assert any(x["kind"] == "INTRABAR_ADVERSE_ESTIMATE" for x in result["equity_curve"])


def test_fresh_quote_rejects_mark_price_crossing_stop_even_if_bid_ask_have_not():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    ticker = {
        "timestamp": int(now.timestamp() * 1000),
        "bid1": 91.0, "ask1": 91.1, "fairPrice": 89.5,
    }
    with pytest.raises(ValueError, match="quote/mark"):
        refresh_trade_levels(
            ticker=ticker, contract={"priceUnit": 0.1}, side="LONG",
            reference_entry=100, stop=90, atr=20, max_entry_drift_atr=1, now=now,
        )


def test_pipeline_persists_candle_history_idempotently():
    from app.db import SessionLocal
    from app.models import Candle
    from app.services.pipeline import _persist_candle_history

    frame = _frame(datetime(2026, 1, 1, tzinfo=timezone.utc), 3, 3600)
    _persist_candle_history("BTC_USDT", "Min60", frame)
    _persist_candle_history("BTC_USDT", "Min60", frame)
    with SessionLocal() as db:
        rows = db.query(Candle).filter_by(symbol="BTC_USDT", timeframe="Min60").all()
    assert len(rows) == 3
    assert rows[0].timestamp is not None


def test_backtest_fails_closed_when_funding_history_is_unavailable(monkeypatch):
    d1 = _frame(datetime(2025, 1, 1, tzinfo=timezone.utc), 210, 86400)
    h4 = _frame(datetime(2025, 1, 1, tzinfo=timezone.utc), 600, 14400)
    h1 = _frame(datetime(2026, 2, 1, tzinfo=timezone.utc), 58, 3600)
    contract = {
        "takerFeeRate": 0.0005, "feeRateMode": "NORMAL", "feeRateType": "BASE",
        "contractSize": 1.0, "volUnit": 1.0, "priceUnit": 0.01,
        "minVol": 1.0, "maxVol": 100.0, "limitMaxVol": 100.0,
        "maxLeverage": 5, "initialMarginRate": 0.2, "maintenanceMarginRate": 0.005,
        "riskLimitMode": "NORMAL",
    }
    with pytest.raises(ValueError, match="cannot assume zero funding"):
        backtest_engine.run_event_backtest(
            symbol="BTC_USDT", candles_1h=h1, candles_4h=h4, candles_1d=d1,
            contract=contract, start_time=h1.iloc[50]["timestamp"],
            end_time=h1.iloc[-1]["timestamp"], funding_events=None,
        )


def test_fresh_quote_rejects_when_original_tp1_already_reached():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    ticker = {"timestamp": int(now.timestamp() * 1000), "bid1": 110.0, "ask1": 110.1}
    with pytest.raises(ValueError, match="Original TP1 has already been reached"):
        refresh_trade_levels(
            ticker=ticker, contract={"priceUnit": 0.1}, side="LONG",
            reference_entry=100, stop=90, reference_tp1=110, reference_tp2=120, atr=20,
            max_entry_drift_atr=2, now=now,
        )


def test_custom_contract_uses_first_tier_initial_and_maintenance_margin():
    from app.risk.manager import resolve_contract_risk_limits

    contract = {
        "riskLimitMode": "CUSTOM",
        "maxVol": 1_000_000,
        "limitMaxVol": 500_000,
        "maxLeverage": 100,
        "riskLimitCustom": [
            {"level": 1, "maxVol": 100, "imr": 0.12, "mmr": 0.08, "maxLeverage": 8},
            {"level": 2, "maxVol": 500, "imr": 0.2, "mmr": 0.1, "maxLeverage": 5},
        ],
    }
    result = resolve_contract_risk_limits(contract)
    assert result["max_contracts"] == 100
    assert result["max_leverage"] == 8
    assert result["initial_margin_rate"] == pytest.approx(0.12)
    assert result["maintenance_margin_rate"] == pytest.approx(0.08)
    assert result["risk_tier_source"] == "CUSTOM_FIRST_RISK_TIER"


def test_custom_contract_without_valid_tier_fails_closed():
    from app.risk.manager import resolve_contract_risk_limits

    with pytest.raises(ValueError, match="CUSTOM risk-limit contract lacks valid first-tier"):
        resolve_contract_risk_limits({
            "riskLimitMode": "CUSTOM", "maxVol": 1000, "limitMaxVol": 100,
            "maxLeverage": 100, "riskLimitCustom": [{"level": 1, "maxVol": 50}],
        })


def test_custom_first_tier_margin_rate_is_used_for_position_size():
    from app.risk.manager import resolve_contract_risk_limits

    contract = {
        "riskLimitMode": "CUSTOM", "maxVol": 1000, "limitMaxVol": 1000,
        "maxLeverage": 100, "contractSize": 1, "volUnit": 1,
        "riskLimitCustom": [
            {"level": 1, "maxVol": 100, "imr": 0.30, "mmr": 0.08, "maxLeverage": 8},
            {"level": 2, "maxVol": 500, "imr": 0.40, "mmr": 0.10, "maxLeverage": 5},
        ],
    }
    limits = resolve_contract_risk_limits(contract)
    plan = size_position(
        equity=10_000, risk_pct=5, entry=100, stop=90, side="LONG",
        contract_size=1, vol_unit=1, leverage=8, taker_fee_rate=0.0,
        slippage_bps=0, initial_margin_rate=limits["initial_margin_rate"],
        maintenance_margin_rate=limits["maintenance_margin_rate"],
        available_margin_usdt=10_000, margin_buffer_pct=0,
        min_contracts=1, max_contracts=limits["max_contracts"],
        max_leverage=limits["max_leverage"],
    )
    assert plan.initial_margin_estimate_usdt == pytest.approx(plan.notional_usdt * 0.30)
    assert plan.contracts <= 100


def test_custom_first_tier_max_leverage_is_enforced():
    from app.risk.manager import resolve_contract_risk_limits

    limits = resolve_contract_risk_limits({
        "riskLimitMode": "CUSTOM", "maxVol": 1000, "limitMaxVol": 1000,
        "maxLeverage": 100,
        "riskLimitCustom": [
            {"level": 1, "maxVol": 100, "imr": 0.2, "mmr": 0.1, "maxLeverage": 8},
        ],
    })
    assert limits["max_leverage"] == 8
    with pytest.raises(ValueError, match="exceeds contract maximum"):
        size_position(
            equity=1000, risk_pct=1, entry=100, stop=95, side="LONG",
            contract_size=1, vol_unit=1, leverage=9, taker_fee_rate=0.001,
            initial_margin_rate=limits["initial_margin_rate"],
            maintenance_margin_rate=limits["maintenance_margin_rate"],
            max_contracts=limits["max_contracts"], max_leverage=limits["max_leverage"],
        )


def test_net_rr_uses_half_tp1_half_tp2_exit_schedule():
    from app.simulation.costs import net_reward_risk

    result = net_reward_risk(
        entry=100, stop=90, target_1=110, target=125, side="LONG",
        target_1_fraction=0.5, contract_size=1, contracts=1,
        taker_fee_rate=0.0, slippage_bps=0.0,
    )
    assert result["gross_rr"] == pytest.approx(1.75)
    assert result["net_rr"] == pytest.approx(1.75)
    assert result["target_1_fraction"] == pytest.approx(0.5)
    assert result["target_2_fraction"] == pytest.approx(0.5)
    assert result["funding_included"] is False
    assert result["cost_basis"].endswith("BEFORE_FUNDING")


def test_portfolio_capacity_rejects_aggregate_margin_and_notional_overcommit():
    from app.risk.manager import validate_portfolio_capacity

    reasons = validate_portfolio_capacity(
        equity=1000, committed_risk=10, proposed_risk=5, max_risk_pct=3,
        committed_margin=700, proposed_margin=250, margin_buffer_pct=10,
        committed_notional=2500, proposed_notional=1000, max_notional_multiple=3,
    )
    assert any("margin reservation" in reason for reason in reasons)
    assert any("notional" in reason for reason in reasons)


def test_portfolio_capacity_accepts_within_limits():
    from app.risk.manager import validate_portfolio_capacity

    assert validate_portfolio_capacity(
        equity=1000, committed_risk=5, proposed_risk=5, max_risk_pct=3,
        committed_margin=300, proposed_margin=300, margin_buffer_pct=10,
        committed_notional=500, proposed_notional=500, max_notional_multiple=3,
    ) == []


def test_funding_exposure_after_tp1_uses_configured_exit_fraction():
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    h1 = pd.DataFrame({
        "timestamp": [start, start + pd.Timedelta(hours=1), start + pd.Timedelta(hours=2)],
        "open": [100.0] * 3, "high": [101.0] * 3, "low": [99.0] * 3,
        "close": [100.0] * 3, "volume": [10.0] * 3, "amount": [1000.0] * 3,
    })
    event = {
        "settleTime": int((start + pd.Timedelta(hours=2)).timestamp() * 1000),
        "fundingRate": 0.01,
        "fairPrice": 100.0,
    }
    cost, status = backtest_engine._funding_cost_until(
        events=[event], start_time=start, end_time=start + pd.Timedelta(hours=3),
        h1=h1, side="LONG", base_quantity=10.0,
        tp1_time=start + pd.Timedelta(hours=1), tp1_exit_fraction=0.25,
    )
    # 25% was closed at TP1; only 75% (7.5 base units) remained at settlement.
    assert cost == pytest.approx(7.5)
    assert status == "HISTORICAL_RATE_WITH_EVENT_FAIR_PRICE"


def test_all_sleeve_curve_retains_failed_symbol_allocation_as_cash():
    from app.services.pipeline import _aggregate_sleeve_equity_curves

    results = [{
        "symbol": "BTC_USDT",
        "equity_curve": [
            {"time": "2026-01-01T01:00:00+00:00", "equity": 525.0, "kind": "REALIZED"},
        ],
    }]
    curve = _aggregate_sleeve_equity_curves(results, starting_equity=1000.0, sleeve_count=2)
    assert curve[0]["equity"] == pytest.approx(1025.0)


def test_mexc_nonretryable_4xx_is_not_retried():
    response = Mock()
    response.status_code = 400
    response.headers = {}
    response.request = httpx.Request("GET", "https://api.mexc.com/example")
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "bad request", request=response.request, response=response,
    )
    http = Mock()
    http.get.return_value = response
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0
    client._max_retries = 3
    with pytest.raises(MexcAPIError, match="Non-retryable MEXC HTTP 400"):
        client._get("/example")
    assert http.get.call_count == 1
    client.close()


def test_mexc_retries_transient_503_then_succeeds():
    def response(status, data=None):
        item = Mock()
        item.status_code = status
        item.headers = {}
        item.request = httpx.Request("GET", "https://api.mexc.com/example")
        item.json.return_value = {"success": True, "data": data}
        item.raise_for_status.return_value = None
        return item

    http = Mock()
    http.get.side_effect = [response(503), response(200, {"ok": True})]
    client = MexcPublicClient(base_url="https://api.mexc.com", client=http)
    client._min_interval = 0
    client._max_retries = 1
    # Avoid real sleep in the retry test.
    import app.market.mexc as mexc_module
    original_sleep = mexc_module.time.sleep
    mexc_module.time.sleep = lambda *_: None
    try:
        assert client._get("/example") == {"ok": True}
    finally:
        mexc_module.time.sleep = original_sleep
        client.close()
    assert http.get.call_count == 2


def test_orderbook_capacity_uses_mexc_third_level_value_as_contract_quantity():
    from app.risk.liquidity import assess_orderbook_capacity

    result = assess_orderbook_capacity(
        depth={"asks": [[100.1, 10, 3], [100.2, 5, 2]], "bids": [[100.0, 9, 10]]},
        side="LONG", mid_price=100.0, contract_size=0.1,
        planned_notional_usdt=40.0, max_distance_bps=25.0,
        minimum_depth_multiple=1.0,
    )
    assert result["side_consumed"] == "asks"
    assert result["depth_notional_usdt_within_band"] == pytest.approx(50.07)
    assert result["depth_to_order_multiple"] == pytest.approx(1.25175)
    assert result["approved"] is True


def test_orderbook_capacity_rejects_insufficient_visible_depth():
    from app.risk.liquidity import assess_orderbook_capacity

    result = assess_orderbook_capacity(
        depth={"bids": [[99.9, 1, 1]], "asks": [[100.1, 1, 1]]},
        side="SHORT", mid_price=100.0, contract_size=1.0,
        planned_notional_usdt=1000.0, max_distance_bps=25.0,
        minimum_depth_multiple=1.0,
    )
    assert result["approved"] is False
    assert result["side_consumed"] == "bids"


def test_orderbook_capacity_fails_closed_on_malformed_mexc_levels():
    from app.risk.liquidity import assess_orderbook_capacity

    with pytest.raises(ValueError, match="Malformed MEXC asks"):
        assess_orderbook_capacity(
            depth={"asks": [[100.0, 100]], "bids": []},
            side="LONG", mid_price=100.0, contract_size=1.0,
            planned_notional_usdt=10.0,
        )


def test_market_snapshot_preserves_zero_funding_rate_not_ticker_fallback():
    from app.db import SessionLocal
    from app.models import FundingObservation
    from app.services.pipeline import _persist_market_observations

    _persist_market_observations(
        "ZERO_FUNDING_TEST_USDT",
        {"bid1": 99.9, "ask1": 100.1, "fundingRate": 0.02},
        {}, {}, {"fundingRate": 0.0},
    )
    with SessionLocal() as db:
        row = db.query(FundingObservation).filter_by(symbol="ZERO_FUNDING_TEST_USDT").one()
    assert row.funding_rate == pytest.approx(0.0)


def test_META_PHONE_NUMBER_ID_precedes_legacy_whatsapp_alias(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "1343308352199390")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1285387147997440")
    cfg = Settings(_env_file=None)
    assert cfg.whatsapp_phone_number_id == "1343308352199390"


def test_ignore_rules_do_not_hide_required_app_artifacts_package():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    gitignore = (root / ".gitignore").read_text()
    dockerignore = (root / ".dockerignore").read_text()
    assert "/artifacts/" in gitignore
    assert "!/app/artifacts/" in gitignore or "!/app/artifacts/**" in gitignore or "/artifacts/" in gitignore
    assert "!app/artifacts/" in dockerignore
    assert "!app/artifacts/**" in dockerignore
