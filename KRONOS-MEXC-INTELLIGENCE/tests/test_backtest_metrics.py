import pytest
from app.backtesting.engine import _metrics


def test_metrics_empty_result():
    result = _metrics([], [], 1000)
    assert result["trades"] == 0
    assert result["net_profit_usdt"] == 0


def test_metrics_wins_losses_and_profit_factor():
    trades = [
        {"net_pnl_usdt": 20, "net_r": 1.5, "side":"LONG", "fees_usdt": 1, "slippage_usdt": 1, "funding_usdt": 0},
        {"net_pnl_usdt": -10, "net_r": -1, "side":"SHORT", "fees_usdt": 1, "slippage_usdt": 1, "funding_usdt": 0},
    ]
    eq = [{"equity":1010}, {"equity":1020}]
    result = _metrics(trades, eq, 1010)
    assert result["wins"] == 1
    assert result["losses"] == 1
    assert result["profit_factor"] == pytest.approx(2.0)
