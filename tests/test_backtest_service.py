from __future__ import annotations

import asyncio
from datetime import timedelta

from app.config import Settings
from app.data.models import Candle
from app.persistence.store import SQLiteStore
import app.backtesting.service as service


class FakeMexcClient:
    instances = []

    def __init__(self, base_url):
        self.base_url = base_url
        self.calls = []
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def fetch_ohlcv(self, symbol, interval, start, end, limit=1000):
        self.calls.append((symbol, interval, start, end, limit))
        bars = []
        current = start
        while current < end:
            bars.append(Candle(
                timestamp=current, open=100.0, high=101.0, low=99.0, close=100.5,
                volume=10.0, close_time=current + timedelta(minutes=5) - timedelta(milliseconds=1),
            ))
            current += timedelta(minutes=5)
        return bars


class FakeEngine:
    calls = 0

    def run_scenarios(self, data, multiples, risk, signal_start, signal_end):
        self.__class__.calls += 1
        assert sorted(data) == ["BTCUSDT", "ETHUSDT"]
        assert tuple(multiples) == (1.0, 1.2, 1.5, 2.0)
        assert risk == 5.0
        assert signal_start < signal_end
        metrics = {
            "signals_total": 0, "entered_trades": 0, "resolved_trades": 0,
            "wins": 0, "losses": 0, "win_rate_pct_resolved_denominator": None,
            "net_pnl_resolved": 0.0, "profit_factor_net_resolved": None,
            "expectancy_r_resolved": None, "max_drawdown_pct_realized": 0.0,
        }
        return {
            f"{multiple:g}R": {"summary": {"comparison": {"target_r_multiple": multiple}}, "metrics": metrics}
            for multiple in multiples
        }


def test_rr_comparison_loads_each_symbol_once_and_runs_one_shared_scenario_pass(monkeypatch):
    FakeMexcClient.instances = []
    FakeEngine.calls = 0
    monkeypatch.setattr(service, "MexcFuturesClient", FakeMexcClient)
    monkeypatch.setattr(service, "_engine_from_settings", lambda settings: FakeEngine())

    async def run():
        store = SQLiteStore(":memory:")
        try:
            settings = Settings(
                symbols=("BTCUSDT", "ETHUSDT"), mexc_base_url="https://contract.mexc.com",
                backtest_fixed_risk_usd=5.0,
            )
            summary = await service.run_rr_comparison(settings, store, settings.symbols, days=1)
            assert len(FakeMexcClient.instances) == 1
            assert len(FakeMexcClient.instances[0].calls) == 2
            assert [call[0] for call in FakeMexcClient.instances[0].calls] == ["BTCUSDT", "ETHUSDT"]
            assert FakeEngine.calls == 1
            assert summary["comparison_method"]["strategy_candidate_generation_passes"] == 1
            assert summary["risk_model"]["gross_reward_by_target_r"] == {
                "1R": 5.0, "1.2R": 6.0, "1.5R": 7.5, "2R": 10.0,
            }
            assert set(summary["scenarios"]) == {"1R", "1.2R", "1.5R", "2R"}
        finally:
            store.close()
    asyncio.run(run())


def test_backtest_report_explains_unfilled_candidates_and_marks_zero_entry_runs_inconclusive():
    scenario = {
        "gross_target_reward_usd_before_costs": 5.0,
        "signals_total": 1,
        "entered_trades": 0,
        "resolved_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate_pct_resolved_denominator": None,
        "net_pnl_resolved": 0.0,
        "profit_factor_net_resolved": None,
        "expectancy_r_resolved": None,
        "max_drawdown_pct_realized": 0.0,
        "entry_status_counts": {"UNFILLED_EXPIRED": 1},
        "entry_status_notes": ["Limit entry did not meet the close-through fill rule before expiry."],
    }
    summary = {
        "symbols": ["BTCUSDT", "ETHUSDT"], "period_days": 7,
        "runtime_seconds": 7.7, "risk_model": {"fixed_gross_stop_risk_usd": 5.0},
        "scenarios": {label: dict(scenario) for label in ("1R", "1.2R", "1.5R", "2R")},
    }
    message = service.format_rr_comparison(summary)
    assert "INCONCLUSIVE: no entries filled" in message
    assert "Entry outcomes (same across TP variants): expired-unfilled=1" in message
    assert "Limit entry did not meet the close-through fill rule before expiry." in message
