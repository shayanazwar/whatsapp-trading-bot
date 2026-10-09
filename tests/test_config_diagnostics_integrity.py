from __future__ import annotations

import pytest

from app.analysis import engine
from app.backtest.report import format_report, summarize
from app.backtest.runner import _merge_symbol_diagnostics, _write_engine_config_diagnostics


def test_config_metadata_is_not_summed_per_symbol():
    totals: dict[str, int] = {}
    per_symbol = {
        "ENGINE_CALLS": 1,
        "V11_MIN_IMPULSE_ATR_X100": 250,
        "V11_MAX_TRIGGER_BARS": 6,
        "V11_SL_MODE_LIQUIDITY_SWEEP": 1,
        "FIXED_BACKTEST_PERIOD_ENABLED": 1,
    }

    # Reproduce the old 200-symbol aggregation scenario.
    for _ in range(200):
        _merge_symbol_diagnostics(totals, per_symbol)

    assert totals["ENGINE_CALLS"] == 200
    assert "V11_MIN_IMPULSE_ATR_X100" not in totals
    assert "V11_MAX_TRIGGER_BARS" not in totals
    assert "V11_SL_MODE_LIQUIDITY_SWEEP" not in totals
    assert "FIXED_BACKTEST_PERIOD_ENABLED" not in totals

    config = engine.engine_config_snapshot()
    _write_engine_config_diagnostics(totals, config, fixed_period_enabled=True)
    assert totals["V11_MIN_IMPULSE_ATR_X100"] == 250
    assert totals["V11_MAX_TRIGGER_BARS"] == 6
    assert totals["V11_SL_MODE_LIQUIDITY_SWEEP"] == 1
    assert totals["FIXED_BACKTEST_PERIOD_ENABLED"] == 1


def test_engine_config_rejects_scaled_impulse_and_trigger_values(monkeypatch):
    monkeypatch.setattr(engine, "V11_MIN_IMPULSE_ATR", 500.0)
    with pytest.raises(ValueError, match="expected 0.25..10.0 ATR"):
        engine.validate_engine_config()

    monkeypatch.setattr(engine, "V11_MIN_IMPULSE_ATR", 2.50)
    monkeypatch.setattr(engine, "V11_MAX_TRIGGER_BARS", 1200)
    with pytest.raises(ValueError, match="expected 1..24 completed 1H bars"):
        engine.validate_engine_config()


def test_report_prints_actual_build_config_and_fingerprint():
    config = engine.engine_config_snapshot()
    summary = summarize(
        days=7,
        period_start_ms=1_790_697_600_000,
        period_end_ms=1_791_302_400_000,
        coins_selected=200,
        coins_tested=200,
        data_errors=0,
        execution_errors=0,
        rejected_setups=0,
        trades=[],
        diagnostics={
            "V11_MIN_IMPULSE_ATR_X100": 250,
            "V11_MAX_TRIGGER_BARS": 6,
            "FIXED_BACKTEST_PERIOD_ENABLED": 1,
        },
        engine_version=config["engine_version"],
        engine_config_fingerprint=config["fingerprint"],
        engine_config=config,
    )
    report = format_report(summary)

    assert f"Engine Build: {config['engine_version']}" in report
    assert "Impulse 2.50 ATR" in report
    assert "Reclaim 6 x 1H" in report
    assert f"Config Fingerprint: {config['fingerprint']}" in report


def test_zero_trade_report_does_not_render_malformed_r_units():
    report = format_report(summarize(
        days=1,
        period_start_ms=0,
        period_end_ms=86_400_000,
        coins_selected=0,
        coins_tested=0,
        data_errors=0,
        execution_errors=0,
        rejected_setups=0,
        trades=[],
    ))
    assert "Avg Realized R: N/A" in report
    assert "Expectancy: N/A" in report
    assert "N/AR" not in report
