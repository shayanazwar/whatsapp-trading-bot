from datetime import datetime, timezone

from rslr.backtest import BacktestTrade, calculate_metrics


def trade(result: str, pnl: float, exit_hour: int) -> BacktestTrade:
    return BacktestTrade(
        symbol="BTC_USDT", side="LONG", module="SWEEP", signal_time="2026-01-01T00:00:00+00:00",
        entry_time="2026-01-01T01:00:00+00:00", exit_time=f"2026-01-01T{exit_hour:02d}:00:00+00:00",
        entry=100, stop=99, target=101, contracts=1, risk_budget_usd=0.5,
        gross_pnl=pnl + 0.01, fees=0.01, slippage=0.0, funding=0.0, net_pnl=pnl,
        result=result, holding_hours=1,
    )


def test_metrics_use_only_resolved_trade_denominator_and_show_open_separately():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    trades = [trade("TP", 0.4, 2), trade("SL", -0.5, 3), trade("OPEN", 0.0, 4)]
    result = calculate_metrics("1D", start, end, 100, trades, 1)
    assert result.tp_hits == 1
    assert result.sl_hits == 1
    assert result.open_unresolved == 1
    assert result.tp_hit_rate_pct == 50.0
    assert abs(result.net_pnl_usd - (-0.1)) < 1e-9


def test_backtest_rejects_unsupported_period():
    from rslr.backtest import run_backtest
    import pytest
    with pytest.raises(ValueError, match="1D, 7D or 30D"):
        run_backtest("5D")


def test_walk_forward_split_is_chronological_and_disjoint():
    from datetime import timedelta
    from types import SimpleNamespace
    from rslr.walk_forward import walk_forward_from_result

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=10)
    trades = [trade("TP", 0.2, 2), trade("SL", -0.1, 9)]
    # Move the second entry into the later chronological segment.
    trades[1].entry_time = "2026-01-09T01:00:00+00:00"
    result = SimpleNamespace(start=start.isoformat(), end=end.isoformat(), period="30D", trades=trades, starting_equity=100, coins_tested=5)
    output = walk_forward_from_result(result)
    assert output["boundary"] == (start + timedelta(days=7)).isoformat()
    assert output["in_sample"]["total_signals"] == 1
    assert output["out_of_sample"]["total_signals"] == 1


def test_generated_signals_are_separate_from_trades_entered():
    import pandas as pd
    from types import SimpleNamespace
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    generated = [
        SimpleNamespace(side="LONG", module="SWEEP"),
        SimpleNamespace(side="SHORT", module="DONCHIAN"),
        SimpleNamespace(side="LONG", module="SWEEP+DONCHIAN"),
        SimpleNamespace(side="LONG", module="SWEEP"),
    ]
    result = calculate_metrics("1D", start, end, 100, [trade("TP", 0.4, 2), trade("OPEN", 0.0, 3)], 8, signals=generated)
    assert result.total_signals == 4
    assert result.trades_taken == 2
    assert result.long_signals == 3
    assert result.short_signals == 1
    assert result.sweep_signals == 3
    assert result.donchian_signals == 2
