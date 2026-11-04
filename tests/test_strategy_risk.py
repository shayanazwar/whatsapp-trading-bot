import pytest
from app.strategy.engine import analyze_multitimeframe
from app.risk.manager import size_position
from app.strategy.engine import StrategyDecision


def test_strategy_returns_insufficient_data_for_short_history(candles_1h):
    short = candles_1h.head(20)
    result = analyze_multitimeframe("BTC_USDT", short, short, short, short)
    assert result.decision == "INSUFFICIENT_DATA"


def test_risk_sizing_stays_within_risk_budget():
    plan = size_position(equity=1000, risk_pct=0.5, entry=100, stop=95, side="LONG",
                         contract_size=0.001, vol_unit=1, leverage=2,
                         taker_fee_rate=0.0006, slippage_bps=3, maintenance_margin_rate=0.005)
    assert plan.contracts > 0
    assert plan.estimated_stop_loss_usdt <= plan.risk_budget_usdt + 1e-8
    assert plan.initial_margin_estimate_usdt > 0


def test_risk_rejects_wrong_stop_geometry():
    with pytest.raises(ValueError):
        size_position(equity=1000, risk_pct=0.5, entry=100, stop=101, side="LONG",
                      contract_size=0.001, vol_unit=1, leverage=2, taker_fee_rate=0.0006)


def test_risk_requires_real_fee_metadata():
    with pytest.raises(ValueError, match="fee"):
        size_position(equity=1000, risk_pct=0.5, entry=100, stop=95, side="LONG",
                      contract_size=0.001, vol_unit=1, leverage=2, taker_fee_rate=None)
