from __future__ import annotations

import pytest

from app import bot as bot_module
from app.analysis import engine


def _candles(count: int, interval_ms: int, now_ms: int):
    rows = []
    for index in range(count):
        close = 100.0 + index * 0.01
        rows.append({
            "time": now_ms - (count - index) * interval_ms,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
        })
    return rows


def test_engine_can_evaluate_long_only_without_calling_short_side(monkeypatch):
    day = 86_400_000
    twelve_hours = 12 * 3_600_000
    four_hours = 4 * 3_600_000
    hour = 3_600_000
    now = (1_800_000_000_000 // day) * day
    c1d = _candles(220, day, now)
    c12 = _candles(80, twelve_hours, now)
    c4 = _candles(220, four_hours, now)
    c1 = _candles(220, hour, now)
    evaluated = []

    monkeypatch.setattr(engine, "_data_quality", lambda *_args: (True, "OK"))
    monkeypatch.setattr(engine, "_v11_regime_1d", lambda _c: {
        "regime": "BULLISH", "bull": True, "bear": False,
        "e21": 101.0, "e50": 100.0, "e200": 99.0,
        "slope": 0.01, "adx": 20.0, "structure": "HH/HL",
        "price": 102.0, "bull_votes": 5, "bear_votes": 1,
    })

    def fake_side(_symbol, side, *_args, **_kwargs):
        evaluated.append(side)
        return {
            "side": side,
            "candidate": False,
            "reason": f"{side}: test rejection",
            "primary_failure": f"{side}: test rejection",
            "diagnostic_key": f"{side}_TEST_REJECTION",
            "technical_gate_failures": [f"{side}: test rejection"],
            "rejection_stage": "SETUP",
        }

    monkeypatch.setattr(engine, "_v11_analyze_side", fake_side)
    result = engine.analyze_candles(
        "LONG_ONLY_TEST", c1d, c12, c4, c1,
        now_ms=now, allow_shorts=False,
    )

    assert evaluated == ["LONG"]
    assert [item["side"] for item in result["side_diagnostics"]] == ["LONG"]


def test_backtest_defaults_to_fixed_15r_and_rejects_other_targets():
    assert bot_module.parse_backtest_args("7D") == ("7D", 7, "1.5R")
    assert bot_module.parse_backtest_args("7D TP=1.5R") == ("7D", 7, "1.5R")
    with pytest.raises(ValueError, match="fixed at 1.5R"):
        bot_module.parse_backtest_args("7D TP=CONTROL")
    with pytest.raises(ValueError, match="fixed at 1.5R"):
        bot_module.parse_backtest_args("7D TP=2.0R")
