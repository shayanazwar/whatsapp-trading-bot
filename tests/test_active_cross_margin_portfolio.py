from __future__ import annotations

from app.backtest import (
    BacktestConfig,
    _cross_margin_snapshot,
    _position_from_candidate,
    run_backtest,
)
from app.market.candles import Candle, aggregate_12h_from_4h
from app.strategy.rfpc import STRATEGY_VERSION, StrategyEvaluation, TradeCandidate
from tests.helpers import candles


def _candidate(symbol: str, decision_time: int) -> TradeCandidate:
    return TradeCandidate(
        candidate_id=f"{symbol}-candidate", strategy_version=STRATEGY_VERSION,
        symbol=symbol, side="LONG", decision_time=decision_time,
        entry_reference=100.0, stop_price=99.0, target_price=102.0,
        initial_risk_distance=1.0, regime="BULLISH",
        setup_pivot_time=decision_time - 8 * 3600,
        breakout_time=decision_time - 4 * 3600,
        reclaim_time=decision_time - 2 * 3600,
        trigger_time=decision_time - 3600, gross_reward_risk=2.0,
        reason="synthetic unit-test candidate", evidence={},
    )


def test_active_position_size_obeys_initial_margin_cap_and_contract_leverage():
    from app.backtest import _position_from_candidate

    start = 1_704_067_200
    candle = Candle("BTC_USDT", "1H", start, 100, 101, 99.5, 100, volume=1)
    spec = {
        "contractSize": 1.0, "volUnit": 0.001, "minVol": 0.001, "maxVol": 100.0,
        "maxLeverage": 20, "maintenanceMarginRate": 0.005,
    }
    cfg = BacktestConfig(
        mode="exchange_realistic", starting_balance=100,
        initial_margin_per_position=1.0, max_leverage=50,
        max_total_notional_multiple=25,
    )
    pos, reason = _position_from_candidate(
        _candidate("BTC_USDT", start + 3600), candle, 0, "setup",
        cfg, 100.0, spec, 0.0005, 0.0008, [],
    )
    assert reason is None
    assert pos is not None
    assert pos.initial_margin <= 1.0 + 1e-9
    assert pos.leverage <= 20
    assert pos.risk_amount <= 0.5 + 1e-9
    assert pos.maintenance_margin_rate == 0.005
    assert pos.risk_tier_status == "BASE_MMR_FALLBACK"


def test_cross_margin_snapshot_uses_shared_wallet_and_all_positions():
    from app.backtest import SimPosition

    positions = [
        SimPosition(
            symbol=f"S{i}_USDT", side="LONG", entry_index=0, entry_time=1000,
            entry_price=100, stop_price=90, target_price=120,
            candidate_id=f"c{i}", setup_key=f"k{i}", risk_amount=1,
            quantity=1, contract_size=1, opening_fee=0.01,
            initial_margin=2, leverage=50, maintenance_margin_rate=0.01,
            liquidation_fee_rate=0.001, mode="exchange_realistic",
        )
        for i in range(3)
    ]
    snapshot = _cross_margin_snapshot(
        100, positions, {f"S{i}_USDT": 95 for i in range(3)}, 0.0008,
    )
    assert snapshot["equity"] < 100
    assert snapshot["maintenance_margin"] == 2.85
    assert abs(snapshot["liquidation_fees"] - 0.285) < 1e-9
    assert snapshot["used_initial_margin"] == 6
    assert snapshot["total_notional"] == 285


def test_portfolio_cross_margin_stress_liquidates_all_positions_before_stops(monkeypatch):
    """Fifty simultaneous positions share one wallet and one portfolio liquidation test."""
    import app.backtest as backtest_module

    day_start = 1_704_067_200
    end = day_start + 220 * 86400
    h1_start = end - 72 * 3600
    symbols = [f"S{i:02d}_USDT" for i in range(50)]
    series_by_symbol = {}
    contract_specs = {}
    for symbol in symbols:
        daily = candles(symbol, "1D", day_start, 220, 86400, base=100, trend=0)
        bars4 = candles(symbol, "4H", end - 180 * 14400, 180, 14400, base=100, trend=0)
        bars1 = candles(symbol, "1H", h1_start, 72, 3600, base=100, trend=0)
        # The first signal generated in the requested day enters on the next hour.
        # The test then applies an extreme adverse move to exercise the shared-wallet
        # maintenance threshold before individual stop handling. This is a synthetic
        # stress fixture, not a claim about normal market movement.
        entry_index = 49
        bars1[entry_index] = Candle(
            symbol, "1H", bars1[entry_index].open_time,
            100.0, 101.0, 80.0, 90.0, volume=1.0,
        )
        series_by_symbol[symbol] = {
            "1D": daily,
            "12H": aggregate_12h_from_4h(bars4),
            "4H": bars4,
            "1H": bars1,
        }
        contract_specs[symbol] = {
            "contractSize": 1.0, "volUnit": 0.001, "minVol": 0.001, "maxVol": 100.0,
            "maxLeverage": 50, "maintenanceMarginRate": 0.005,
        }

    emitted: set[str] = set()

    def emit_once(symbol, series, *, decision_time, **kwargs):
        candidate = None
        if symbol not in emitted:
            emitted.add(symbol)
            candidate = _candidate(symbol, decision_time)
        return StrategyEvaluation(
            symbol=symbol, decision_time=decision_time, regime="BULLISH",
            candidate=candidate, rejection_reasons=(), evidence={},
        )

    monkeypatch.setattr(backtest_module, "evaluate_strategy", emit_once)
    cfg = BacktestConfig(
        mode="exchange_realistic", starting_balance=100,
        risk_fraction=0.0045, max_concurrent_positions=50,
        max_open_risk_fraction=0.25, initial_margin_per_position=1.0,
        max_leverage=50, max_total_notional_multiple=25,
        max_holding_hours=48,
    )
    result = run_backtest(
        series_by_symbol, window="1D", config=cfg,
        contract_specs=contract_specs, now_epoch=end,
    )

    assert result["candidate_signals"] == 50
    assert result["positions_entered"] == 50
    assert result["max_observed_concurrent_positions"] == 50
    assert result["liquidation_events"] == 1
    assert result["liquidation_exits"] == 50
    assert result["open_trades_at_end"] == 0
    assert result["liquidation_modelled"] is True
    assert result["exchange_exact_cross_margin_modelled"] is False
    assert result["liquidation_price_basis"] == "1H_TRADE_PRICE_OHLC_CONSERVATIVE_STRESS_PROXY"
    assert result["reconciliation"]["status"] == "PASS"
    assert len(result["trades"]) == 50
    assert all(row["margin_mode"] == "cross" for row in result["trades"])
    assert all(row["initial_margin"] <= 1.0 + 1e-9 for row in result["trades"])
    assert all(row["quantity"] > 0 and row["leverage"] <= 50 for row in result["trades"])


def test_backtest_final_open_position_is_marked_to_market_and_metrics_reconcile(monkeypatch):
    import app.backtest as backtest_module

    day_start = 1_704_067_200
    end = day_start + 220 * 86400
    symbol = "OPEN_USDT"
    daily = candles(symbol, "1D", day_start, 220, 86400, base=100, trend=0)
    bars4 = candles(symbol, "4H", end - 180 * 14400, 180, 14400, base=100, trend=0)
    bars1 = candles(symbol, "1H", end - 72 * 3600, 72, 3600, base=100, trend=0)
    # Raise final bars so the last open position has positive final unrealized P&L.
    for idx in range(48, 72):
        bar = bars1[idx]
        bars1[idx] = Candle(symbol, "1H", bar.open_time, 100.0, 101.0, 100.2, 100.8, volume=1)
    series = {symbol: {"1D": daily, "12H": aggregate_12h_from_4h(bars4), "4H": bars4, "1H": bars1}}
    spec = {symbol: {"contractSize": 1.0, "volUnit": 0.001, "minVol": 0.001,
                     "maxVol": 100.0, "maxLeverage": 50, "maintenanceMarginRate": 0.005}}
    emitted = False

    def emit_once(sym, tf, *, decision_time, **kwargs):
        nonlocal emitted
        candidate = None
        if not emitted:
            emitted = True
            candidate = _candidate(sym, decision_time)
        return StrategyEvaluation(sym, decision_time, "BULLISH", candidate, (), {})

    monkeypatch.setattr(backtest_module, "evaluate_strategy", emit_once)
    result = run_backtest(
        series, window="1D", config=BacktestConfig(mode="exchange_realistic", max_holding_hours=100),
        contract_specs=spec, now_epoch=end,
    )
    assert result["open_trades_at_end"] == 1
    assert result["ending_equity"] == result["ending_balance"] + result["final_unrealized_pnl"]
    assert result["reconciliation"]["status"] == "PASS"
    assert result["average_mae_r"] == 0.0  # includes the open trade's excursion-to-date
    assert result["signals_not_entered"] == 0


def test_active_sizer_rejects_missing_contract_margin_or_leverage_metadata():
    start = 1_704_067_200
    candle = Candle("BTC_USDT", "1H", start, 100, 101, 99.5, 100, volume=1)
    cfg = BacktestConfig(mode="exchange_realistic")
    base = {"contractSize": 1.0, "volUnit": 0.001, "minVol": 0.001, "maxVol": 100.0}
    position, reason = _position_from_candidate(
        _candidate("BTC_USDT", start + 3600), candle, 0, "setup", cfg, 100,
        {**base, "maxLeverage": 20}, 0.0005, 0.0008, [],
    )
    assert position is None
    assert reason == "MISSING_MAINTENANCE_MARGIN_METADATA"

    position, reason = _position_from_candidate(
        _candidate("BTC_USDT", start + 3600), candle, 0, "setup", cfg, 100,
        {**base, "maintenanceMarginRate": 0.005}, 0.0005, 0.0008, [],
    )
    assert position is None
    assert reason == "MISSING_CONTRACT_MAX_LEVERAGE"


def test_report_reconciliation_rebuilds_open_unrealized_pnl_independently():
    from app.backtest import SimPosition, _metrics

    position = SimPosition(
        symbol="BTC_USDT", side="LONG", entry_index=0, entry_time=1000,
        entry_price=100.0, stop_price=95.0, target_price=110.0,
        candidate_id="c1", setup_key="s1", risk_amount=0.5,
        quantity=1.0, contract_size=1.0, opening_fee=0.08,
        mode="exchange_realistic", estimated_close_fee=0.08,
        estimated_close_slippage=0.05, final_mark_price=102.0,
    )
    # Gross open P&L 2.00 less opening fee, estimated exit fee and slippage = 1.79.
    correct = _metrics(
        window="1D", mode="exchange_realistic", start_time=1000, end_time=2000,
        starting_balance=100.0, ending_balance=100.0, trades=[],
        open_positions=[position], equity_curve=[100.0, 101.79],
        symbols_tested=1, symbols_eligible=1, valid_candles=24,
        rejection_counts={}, warnings=[], positions_entered=1,
        candidate_signals=1, long_signals=1, final_unrealized_pnl=1.79,
    )
    assert correct["reconciliation"]["status"] == "PASS"

    incorrect = _metrics(
        window="1D", mode="exchange_realistic", start_time=1000, end_time=2000,
        starting_balance=100.0, ending_balance=100.0, trades=[],
        open_positions=[position], equity_curve=[100.0, 110.0],
        symbols_tested=1, symbols_eligible=1, valid_candles=24,
        rejection_counts={}, warnings=[], positions_entered=1,
        candidate_signals=1, long_signals=1, final_unrealized_pnl=10.0,
    )
    assert incorrect["status"] == "INVALID_RECONCILIATION"
    assert incorrect["reconciliation"]["status"] == "FAIL"


def test_contract_risk_tier_helper_uses_custom_and_increase_metadata():
    from app.risk import contract_risk_tier_terms

    custom = contract_risk_tier_terms(
        {
            "riskLimitMode": "CUSTOM", "riskLimitType": "BY_VALUE",
            "riskLimitCustom": [
                {"level": 1, "maxVol": 1000, "mmr": 0.005, "imr": 0.01, "maxLeverage": 100},
                {"level": 2, "maxVol": 5000, "mmr": 0.01, "imr": 0.02, "maxLeverage": 50},
            ],
        }, direction="LONG", quantity=5, notional=800,
        base_mmr=0.005, contract_max_leverage=100,
    )
    assert custom["status"] == "CONTRACT_METADATA_TIER"
    assert custom["tier"] == 1
    assert custom["mmr"] == 0.005
    assert custom["max_leverage"] == 100

    increased = contract_risk_tier_terms(
        {
            "riskLimitMode": "INCREASE", "riskLimitType": "BY_VOLUME",
            "riskBaseVol": 1000, "riskIncrVol": 500,
            "riskLevelLimit": 4, "riskIncrMmr": 0.001,
            "initialMarginRate": 0.01, "riskIncrImr": 0.005,
        }, direction="SHORT", quantity=1200, notional=1200,
        base_mmr=0.005, contract_max_leverage=50,
    )
    assert increased["status"] == "CONTRACT_METADATA_TIER"
    assert increased["tier"] == 2
    assert abs(increased["mmr"] - 0.006) < 1e-12
    assert increased["max_leverage"] == 50

    over_limit = contract_risk_tier_terms(
        {
            "riskLimitMode": "INCREASE", "riskLimitType": "BY_VOLUME",
            "riskBaseVol": 1000, "riskIncrVol": 500,
            "riskLevelLimit": 4, "riskIncrMmr": 0.001,
            "initialMarginRate": 0.01, "riskIncrImr": 0.005,
        }, direction="LONG", quantity=3000, notional=3000,
        base_mmr=0.005, contract_max_leverage=50,
    )
    assert over_limit["status"] == "OVER_LIMIT"


def test_settings_keep_research_cap_at_50_and_reject_isolated_active_backtest(monkeypatch):
    import pytest
    from app.config import load_settings

    monkeypatch.setenv("BACKTEST_MARGIN_MODE", "cross")
    monkeypatch.setenv("BACKTEST_MAX_POSITIONS", "50")
    monkeypatch.setenv("MAX_CONCURRENT_POSITIONS", "1")
    settings = load_settings()
    assert settings.backtest_max_positions == 50
    assert settings.max_concurrent_positions == 1  # separate legacy paper engine remains capped at one

    monkeypatch.setenv("BACKTEST_MARGIN_MODE", "isolated")
    with pytest.raises(ValueError, match="supports BACKTEST_MARGIN_MODE=cross only"):
        load_settings()


def test_mae_mfe_on_exit_bar_only_uses_price_known_before_close():
    from app.backtest import SimPosition, _update_excursion_at_price

    position = SimPosition(
        symbol="TEST_USDT", side="LONG", entry_index=0, entry_time=1000,
        entry_price=100.0, stop_price=95.0, target_price=110.0,
        candidate_id="c1", setup_key="s1", risk_amount=1.0,
        quantity=1.0, contract_size=1.0,
    )
    # If the candle later rallied to 120 after the stop had already exited at 95,
    # that later high must not be included in this trade's MFE.
    _update_excursion_at_price(position, 95.0)
    assert position.mae_r == 5.0
    assert position.mfe_r == 0.0


def test_mae_mfe_open_bar_records_both_extremes_only_for_surviving_position():
    from app.backtest import SimPosition, _update_excursions_from_open_bar

    position = SimPosition(
        symbol="TEST_USDT", side="LONG", entry_index=0, entry_time=1000,
        entry_price=100.0, stop_price=90.0, target_price=130.0,
        candidate_id="c1", setup_key="s1", risk_amount=1.0,
        quantity=1.0, contract_size=1.0,
    )
    bar = Candle("TEST_USDT", "1H", 3600, 100.0, 108.0, 96.0, 104.0, volume=1.0)
    _update_excursions_from_open_bar(position, bar)
    assert position.mae_r == 4.0
    assert position.mfe_r == 8.0


def test_liquidation_exit_timestamps_distinguish_bar_open_from_ohlc_stress():
    from app.backtest import SimPosition, _close_position

    bar = Candle("TEST_USDT", "1H", 7200, 100.0, 105.0, 80.0, 90.0, volume=1.0)
    config = BacktestConfig(mode="exchange_realistic")

    def make_position(candidate_id: str) -> SimPosition:
        return SimPosition(
            symbol="TEST_USDT", side="LONG", entry_index=0, entry_time=3600,
            entry_price=100.0, stop_price=95.0, target_price=110.0,
            candidate_id=candidate_id, setup_key=candidate_id, risk_amount=1.0,
            quantity=1.0, contract_size=1.0, mode="exchange_realistic",
            liquidation_fee_rate=0.0,
        )

    at_open = _close_position(
        make_position("open"), bar, 100.0, "LIQUIDATION_CROSS_PORTFOLIO_AT_OPEN",
        config, 0.0008, 0.0005, None,
    )
    intrabar = _close_position(
        make_position("intrabar"), bar, 80.0, "LIQUIDATION_CROSS_PORTFOLIO_OHLC_STRESS",
        config, 0.0008, 0.0005, None,
    )
    assert at_open.exit_time == bar.open_time
    assert intrabar.exit_time == bar.close_time
