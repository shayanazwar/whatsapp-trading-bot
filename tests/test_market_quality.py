from __future__ import annotations

import pytest

from app.backtest_rsi_ema import _net_exit_pnl
from app.services.market_quality import net_reward_risk_after_costs


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_live_signal_net_rr_matches_backtest_two_sided_fee_slippage_model(direction):
    entry = 100.0
    if direction == "LONG":
        stop, target = 98.5, 102.5
        entry_fill = entry * 1.0005
    else:
        stop, target = 101.5, 97.5
        entry_fill = entry * 0.9995
    fee = 0.0008
    slip = 0.0005
    result = net_reward_risk_after_costs(
        direction=direction, entry_price=entry, stop_price=stop, target_price=target,
        fee_rate_per_side=fee, slippage_bps_per_side=5.0,
    )
    target_outcome = _net_exit_pnl(
        direction=direction, ref_entry=entry, entry_fill=entry_fill,
        exit_ref=target, quantity_units=1.0, fee_rate=fee, slippage_rate=slip,
    )
    stop_outcome = _net_exit_pnl(
        direction=direction, ref_entry=entry, entry_fill=entry_fill,
        exit_ref=stop, quantity_units=1.0, fee_rate=fee, slippage_rate=slip,
    )
    expected = target_outcome["net_pnl"] / abs(stop_outcome["net_pnl"])
    assert result["net_rr"] == pytest.approx(expected)


def test_signal_net_rr_returns_zero_when_costs_destroy_reward():
    result = net_reward_risk_after_costs(
        direction="LONG", entry_price=100.0, stop_price=99.9, target_price=100.01,
        fee_rate_per_side=0.001, slippage_bps_per_side=10.0,
    )
    assert result["net_rr"] == 0.0


def test_signal_net_rr_rejects_invalid_direction_and_nonfinite_inputs():
    with pytest.raises(ValueError, match="direction"):
        net_reward_risk_after_costs(
            direction="FLAT", entry_price=100, stop_price=99, target_price=101,
            fee_rate_per_side=0.0008, slippage_bps_per_side=5,
        )
    with pytest.raises(ValueError, match="finite"):
        net_reward_risk_after_costs(
            direction="LONG", entry_price=float("nan"), stop_price=99, target_price=101,
            fee_rate_per_side=0.0008, slippage_bps_per_side=5,
        )
