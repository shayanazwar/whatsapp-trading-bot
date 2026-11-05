from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.backtest import BacktestConfig, SimPosition, _close_position
from app.market.candles import Candle
from app.risk import RiskConfig, RiskEngine, paper_ledger_risk_metrics
from app.services.market_quality import (
    candle_window_coverage,
    conservative_paper_funding_pnl,
    funding_events_for_window,
    signal_data_quality_gate,
    validate_usdt_perpetual_contract,
)
from app.services.scheduler import ResearchScheduler
from app.storage import Store
from app.strategy.rfpc import evaluate_strategy


def _candle(symbol: str, timeframe: str, opened: int, *, complete: bool = True) -> Candle:
    step = {"1D": 86400, "12H": 43200, "4H": 14400, "1H": 3600}[timeframe]
    return Candle(
        symbol=symbol, timeframe=timeframe, open_time=opened,
        open=100.0, high=101.0, low=99.0, close=100.5,
        complete=complete, source="forensic-test",
    )


def _valid_contract() -> dict:
    return {
        "state": 0, "futureType": 1, "quoteCoin": "USDT", "settleCoin": "USDT",
        "positionOpenType": 3, "contractSize": 0.001, "volUnit": 1,
        "minVol": 1, "maxVol": 100000, "priceUnit": 0.01,
        "maintenanceMarginRate": 0.005, "countryConfigContractMaxLeverage": 100,
        "apiAllowed": True,
    }



def test_wider_funding_history_can_prove_shorter_window_coverage():
    now = 2_000_000_000
    wider_start = now - 365 * 86400
    seven_day_start = now - 7 * 86400
    events = [
        {"settleTime": seven_day_start - 3600, "fundingRate": 0.0001},
        {"settleTime": now - 3600, "fundingRate": -0.0001},
    ]
    sliced, covered = funding_events_for_window(
        events, history_complete=False, requested_start=seven_day_start,
        requested_end=now,
    )
    assert covered is True
    assert len(sliced) == 1
    assert sliced[0]["settleTime"] == now - 3600

    _, long_covered = funding_events_for_window(
        events, history_complete=False, requested_start=wider_start,
        requested_end=now,
    )
    assert long_covered is False

def test_candle_window_coverage_rejects_missing_interval_in_any_strategy_timeframe():
    start = 1_767_225_600  # UTC midnight
    end = start + 86400
    series = {
        "1D": [_candle("BTC_USDT", "1D", start)],
        "12H": [_candle("BTC_USDT", "12H", start + i * 43200) for i in range(2)],
        "4H": [_candle("BTC_USDT", "4H", start + i * 14400) for i in range(6)],
        "1H": [_candle("BTC_USDT", "1H", start + i * 3600) for i in range(24)],
    }
    good = candle_window_coverage(series, start_time=start, end_time=end)
    assert all(row["continuous"] and row["coverage_pct"] == 100.0 for row in good.values())

    series["1H"].pop(5)
    bad = candle_window_coverage(series, start_time=start, end_time=end)
    assert bad["1H"]["coverage_pct"] == pytest.approx(95.8333, abs=1e-3)
    assert bad["1H"]["continuous"] is False
    assert bad["1H"]["missing"] == 1


def test_candle_window_coverage_rejects_off_grid_timestamp():
    start = 1_767_225_600
    end = start + 86400
    series = {
        "1D": [_candle("BTC_USDT", "1D", start)],
        "12H": [_candle("BTC_USDT", "12H", start + i * 43200) for i in range(2)],
        "4H": [_candle("BTC_USDT", "4H", start + i * 14400) for i in range(6)],
        "1H": [_candle("BTC_USDT", "1H", start + i * 3600) for i in range(23)]
        + [_candle("BTC_USDT", "1H", start + 23 * 3600 + 1)],
    }
    result = candle_window_coverage(series, start_time=start, end_time=end)
    assert result["1H"]["continuous"] is False
    assert result["1H"]["off_grid"] == 1
    assert result["1H"]["missing"] == 1


def test_signal_data_quality_gate_fails_closed_for_warnings_or_invalid_contract():
    assert signal_data_quality_gate([], _valid_contract()) == (True, "OK")
    ok, reason = signal_data_quality_gate(["1H gap detected"], _valid_contract())
    assert not ok and "MARKET_DATA_WARNINGS" in reason
    ok, reason = signal_data_quality_gate([], {**_valid_contract(), "settleCoin": "BTC"})
    assert not ok and reason == "CONTRACT_NOT_USDT_SETTLED"
    ok, reason = signal_data_quality_gate([], {**_valid_contract(), "apiAllowed": False})
    assert not ok and reason == "CONTRACT_API_NOT_ALLOWED"
    assert validate_usdt_perpetual_contract(_valid_contract()) == (True, "OK")


def test_conservative_funding_charges_adverse_inside_exit_bar_but_not_favorable_inside():
    events = [
        {"settleTime": 1010, "fundingRate": 0.001},   # adverse for a long, inside candle
        {"settleTime": 1020, "fundingRate": -0.001},  # favorable for a long, inside candle
    ]
    pnl = conservative_paper_funding_pnl(
        events, side="LONG", entry_time=900, candle_open_time=1000,
        candle_close_time=1060, entry_notional=1000,
    )
    assert pnl == pytest.approx(-1.0)


def test_paper_close_requests_funding_through_exit_candle_close_and_records_close_time():
    class FakeMexc:
        def __init__(self):
            self.request = None

        async def funding_history_window_with_coverage(self, symbol, *, start, end, max_pages):
            self.request = (symbol, start, end, max_pages)
            return ([{"settleTime": candle.open_time + 1800, "fundingRate": 0.001}], True)

    class FakePaper:
        def __init__(self):
            self.close_args = None

        def close_position(self, position_id, **kwargs):
            self.close_args = (position_id, kwargs)
            return True

    class FakeStore:
        def __init__(self):
            self.events = []

        def append_event(self, event_type, payload, **kwargs):
            self.events.append((event_type, payload))
            return "event"

    candle = _candle("BTC_USDT", "1H", 3600)
    fake_mexc, fake_paper, fake_store = FakeMexc(), FakePaper(), FakeStore()
    scheduler = ResearchScheduler.__new__(ResearchScheduler)
    scheduler.settings = SimpleNamespace(
        mexc_price_series="standard", backtest_slippage_bps_per_side=0.0,
        exchange_taker_fee_rate=0.0, whatsapp_allowed_senders=(),
    )
    scheduler.mexc = fake_mexc
    scheduler.paper = fake_paper
    scheduler.store = fake_store
    position = {
        "id": "paper-1", "symbol": "BTC_USDT", "side": "LONG", "quantity": 1.0,
        "entry_time": 1800, "entry_price": 100.0,
    }
    import asyncio
    asyncio.run(scheduler._close_simulated(
        position, candle, 99.0, "STOP", {"contract_size": 1.0, "fee_rate": 0.0, "opening_fee": 0.0},
        candle.close_time,
    ))
    assert fake_mexc.request == ("BTC_USDT", 1800, candle.close_time, 5)
    assert fake_paper.close_args[1]["exit_time"] == candle.close_time
    components = next(payload for event, payload in fake_store.events if event == "PAPER_PNL_COMPONENTS_SIMULATED")
    assert components["funding_pnl"] == pytest.approx(-0.1)
    assert components["funding_history_complete"] is True


def test_paper_ledger_calculates_daily_loss_and_realized_drawdown():
    day_start = 1_767_225_600
    positions = [
        {"status": "closed", "exit_time": day_start - 10, "realised_pnl": -50.0},
        {"status": "closed", "exit_time": day_start + 10, "realised_pnl": -20.0},
        {"status": "closed", "exit_time": day_start + 20, "realised_pnl": 5.0},
        {"status": "open", "exit_time": None, "realised_pnl": None},
    ]
    metrics = paper_ledger_risk_metrics(
        positions, starting_balance=1000.0, now_epoch=day_start + 100,
    )
    assert metrics["daily_loss_fraction"] == pytest.approx(15.0 / 950.0)
    assert metrics["current_drawdown_fraction"] == pytest.approx(0.065)
    assert metrics["today_realised_pnl"] == pytest.approx(-15.0)


def test_risk_engine_rejects_daily_loss_and_drawdown_when_ledger_metrics_reach_limits():
    from tests.test_risk_store_whatsapp import sample_candidate

    engine = RiskEngine(RiskConfig(max_daily_loss_fraction=0.02, max_drawdown_fraction=0.10))
    contract = {"state": 0, "apiAllowed": True, "contractSize": 0.001,
                "volUnit": 1, "minVol": 1, "maxVol": 100000, "priceUnit": 0.01}
    daily = engine.evaluate(sample_candidate(), equity=1000, contract=contract, daily_loss_fraction=0.02)
    assert daily.reason == "DAILY_LOSS_LIMIT"
    drawdown = engine.evaluate(
        sample_candidate(), equity=1000, contract=contract, daily_loss_fraction=0.0,
        current_drawdown_fraction=0.10,
    )
    assert drawdown.reason == "DRAWDOWN_LIMIT"


def test_pending_inbound_keyset_pages_recover_backlog_without_offset_skips(tmp_path):
    store = Store(tmp_path / "inbox-pages.sqlite3")
    for index in range(560):
        message_id = f"wamid.{index:04d}"
        assert store.save_inbound(message_id, "923000000001", "HELP", {"messages": []})
    first = store.pending_inbound(limit=500)
    assert len(first) == 500
    for row in first:
        store.mark_inbound_processed(row["message_id"], "test_page_1")
    cursor = (first[-1]["received_at"], first[-1]["message_id"])
    second = store.pending_inbound(limit=500, after=cursor)
    assert len(second) == 60
    assert len({row["message_id"] for row in first + second}) == 560


def test_completed_backtest_job_cannot_be_overwritten_by_later_notification_failure(tmp_path):
    store = Store(tmp_path / "job-terminal-state.sqlite3")
    job_id = store.enqueue_backtest_job("923000000001", {"window": "1D"}, idempotency_key="job-1")
    assert store.claim_next_backtest_job("worker") is not None
    assert store.finish_backtest_job(job_id, result={"text": "real result"}) is True
    assert store.finish_backtest_job(job_id, error="Meta notification failed") is False
    job = store.get_backtest_job(job_id)
    assert job["status"] == "complete"
    assert job["result"]["text"] == "real result"
    assert job["error"] is None


def test_incomplete_only_1h_series_returns_rejection_instead_of_max_value_error():
    decision = 1_767_225_600
    series = {
        "1D": [], "12H": [], "4H": [],
        "1H": [_candle("BTC_USDT", "1H", decision - 3600, complete=False)],
    }
    result = evaluate_strategy("BTC_USDT", series)
    assert result.candidate is None
    assert result.rejection_reasons == ("NO_CLOSED_1H_DATA",)


def test_backtest_holding_period_accepts_fractional_hour_precision():
    config = BacktestConfig(max_holding_hours=75 / 60.0)
    assert config.max_holding_hours == pytest.approx(1.25)


def test_backtest_intrabar_exit_timestamp_is_bar_close_not_bar_open():
    from app.backtest import _close_position

    start = 1_767_225_600
    candle = _candle("BTC_USDT", "1H", start)
    position = SimPosition(
        symbol="BTC_USDT", side="LONG", entry_index=0, entry_time=start - 3600,
        entry_price=100.0, stop_price=99.0, target_price=102.0,
        candidate_id="candidate", setup_key="setup", risk_amount=5.0,
    )
    intrabar = _close_position(position, candle, 99.0, "STOP", BacktestConfig(), 0.0, 0.0, None)
    gap = _close_position(position, candle, candle.open, "STOP_GAP", BacktestConfig(), 0.0, 0.0, None)
    assert intrabar.exit_time == candle.close_time
    assert gap.exit_time == candle.open_time

@pytest.mark.asyncio
async def test_market_data_service_reports_duplicate_candle_opens():
    from app.services.market_data import MarketDataService

    class FakeMexc:
        def __init__(self):
            self.rows = {}
            base = 1_700_000_000
            daily_step, h4_step, h1_step = 86400, 14400, 3600
            self.rows["1D"] = [
                _candle("BTC_USDT", "1D", ((base // daily_step) - 200 + i) * daily_step)
                for i in range(201)
            ]
            self.rows["4H"] = [
                _candle("BTC_USDT", "4H", (base // h4_step - 6 + i) * h4_step)
                for i in range(6)
            ]
            first_h1 = (base // h1_step - 3) * h1_step
            self.rows["1H"] = [
                _candle("BTC_USDT", "1H", first_h1),
                _candle("BTC_USDT", "1H", first_h1 + h1_step),
                _candle("BTC_USDT", "1H", first_h1 + h1_step),
            ]

        async def get_klines_window(self, symbol, timeframe, **kwargs):
            return self.rows[timeframe]

        async def contract_detail(self, symbol):
            return _valid_contract()

    data = await MarketDataService(FakeMexc(), price_series="standard").fetch_symbol(
        "BTC_USDT", window_days=1, daily_warmup_days=200,
        extra_intraday_warmup_days=1, now_epoch=1_700_000_000,
    )
    assert any("duplicate open(s)" in warning for warning in data.warnings)


def test_simultaneous_realised_closes_are_aggregated_for_drawdown():
    timestamp = 1_767_225_600 + 10
    positions = [
        {"status": "closed", "exit_time": timestamp, "realised_pnl": -30.0},
        {"status": "closed", "exit_time": timestamp, "realised_pnl": 30.0},
    ]
    metrics = paper_ledger_risk_metrics(
        positions, starting_balance=1000.0, now_epoch=timestamp + 1,
    )
    assert metrics["realised_equity"] == pytest.approx(1000.0)
    assert metrics["current_drawdown_fraction"] == pytest.approx(0.0)


def test_duplicate_pending_webhook_dispatch_is_guarded_per_process(monkeypatch):
    import asyncio
    import app.main as main_module

    dispatched = []

    async def fake_process(app, message, message_id):
        dispatched.append(message_id)

    scheduled = []
    monkeypatch.setattr(main_module, "_process_one_message", fake_process)
    monkeypatch.setattr(main_module, "_spawn_task", lambda app, coroutine, *, name: scheduled.append(coroutine))
    state = SimpleNamespace(
        settings=SimpleNamespace(whatsapp_allowed_senders=("923001234567",)),
        inbound_dispatching=set(),
    )
    app = SimpleNamespace(state=state)
    message = {"body": "HELP"}
    main_module._route_allowed_message(app, message, "wamid.same")
    main_module._route_allowed_message(app, message, "wamid.same")
    assert len(scheduled) == 1
    assert "wamid.same" in state.inbound_dispatching
    asyncio.run(scheduled[0])
    assert dispatched == ["wamid.same"]
    assert "wamid.same" not in state.inbound_dispatching
