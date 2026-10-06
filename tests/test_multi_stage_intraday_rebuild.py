from __future__ import annotations

import pytest

from app.analysis.engine import APPROVED_TIMEFRAMES, synthesize_12h_from_4h
from app.automation.signal_validator import validate_signal


def candle(ts, price):
    return [ts, price, price + 1, price - 1, price, 1000.0]


def test_strategy_uses_only_1d_12h_4h_1h():
    assert APPROVED_TIMEFRAMES == ("1D", "12H", "4H", "1H")


def test_12h_is_built_only_from_three_contiguous_completed_4h_bars():
    base = (1_700_000_000_000 // 43_200_000) * 43_200_000
    rows = [candle(base + i * 14_400_000, 100 + i) for i in range(4)]
    out = synthesize_12h_from_4h(rows, now_ms=base + 43_200_000 + 1)
    assert len(out) == 1
    assert out[0]["open"] == pytest.approx(100)
    assert out[0]["close"] == pytest.approx(102)
    assert out[0]["volume"] == pytest.approx(3000)


def valid_analysis():
    import time
    now = int(time.time() * 1000)
    return {
        "symbol": "BTC_USDT", "setup": "LONG", "candle_time": now,
        "setup_bos_time": 1_699_992_800_000, "bos_4h_level": 100.0,
        "score": 80, "direction_ok": True, "structure_ok": True, "setup_ok": True,
        "confirmation_ok": True, "location_ok": True, "target_path_structural": True,
        "structure_quality_ok": True, "shock_veto_ok": True, "technical_candidate": True,
        "trade_geometry_ok": True, "risk_ok": True, "primary_entry_timeframe": "1H",
        "signal_candle_timeframe": "1H", "entry": 110.0, "stop_loss": 104.0, "tp": 128.0,
        "rr": 3.0, "atr": 4.0, "sl_atr": 1.5, "tp_distance_atr": 4.5, "mexc_spread_pct": 0.01,
        "max_allowed_spread_pct": 0.50, "max_signal_age_seconds": 5400,
    }


def test_signal_validator_requires_1h_entry_and_structural_geometry():
    data = valid_analysis()
    signal, reasons = validate_signal(data, min_confluence=65, min_rr=2.0)
    assert signal is not None, reasons
    assert signal.analysis["primary_entry_timeframe"] == "1H"


def test_signal_validator_rejects_non_1h_entry_timeframe():
    data = valid_analysis()
    data["primary_entry_timeframe"] = "15M"
    signal, reasons = validate_signal(data, min_confluence=65, min_rr=2.0)
    assert signal is None
    assert any("Primary entry timeframe" in r for r in reasons)
