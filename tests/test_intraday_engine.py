from __future__ import annotations

import math

import pytest

import app.analysis.engine as engine
from app.analysis.engine import _v11_find_impulses, _v11_liquidity_trigger, _v11_target_path


@pytest.fixture(autouse=True)
def _legacy_control_mode(monkeypatch):
    """These legacy fixture tests validate the pre-accuracy V11 control path."""
    monkeypatch.setenv("V11_ACCURACY_MODE", "false")
    monkeypatch.delenv("V11_SL_MODE", raising=False)
    monkeypatch.delenv("V11_MAX_TRIGGER_BARS", raising=False)
    monkeypatch.delenv("V11_MAX_RECLAIM_RECENCY_BARS", raising=False)
    monkeypatch.delenv("V11_MAX_ENTRY_EXTENSION_ATR", raising=False)
    monkeypatch.delenv("V11_MAX_TARGET_R", raising=False)
    monkeypatch.delenv("V11_MAX_RETEST_DEPTH", raising=False)
    monkeypatch.delenv("V11_REQUIRE_BREAKOUT_QUALITY", raising=False)


def candle(ts: int, o: float, h: float, l: float, c: float, v: float = 1000.0):
    return [ts, o, h, l, c, v]


def _mirror(rows: list, pivot: float = 500.0) -> list:
    out = []
    for row in rows:
        ts, o, h, l, c, v = row
        out.append(candle(ts, pivot - o, pivot - l, pivot - h, pivot - c, v))
    return out


def _v11_fixture():
    hour = 3_600_000
    four = 14_400_000
    day = 86_400_000
    now = 1_760_000_000_000

    daily = []
    base = now - 250 * day
    for i in range(250):
        p = 100 + 0.25 * i + 4 * math.sin(i / 6)
        daily.append(candle(base + i * day, p - 0.2, p + 0.8, p - 0.8, p, 1000))

    c4 = []
    base4 = ((now // four) - 220) * four
    for i in range(220):
        if i < 160:
            p = 130 + 0.08 * i + 2 * math.sin(i / 5)
        elif i < 170:
            p = 140 + (i - 160) * 1.2
        elif i < 180:
            p = 152 - (i - 170) * 0.6
        elif i < 191:
            p = 146 + (i - 180) * 2.2
        elif i < 194:
            p = 170 - (i - 191) * 1.0
        else:
            p = 167 - (i - 194) * 0.5
        c4.append(candle(base4 + i * four, p - 0.2, p + 0.7, p - 0.7, p, 1000))
    c4[192] = candle(c4[192][0], 170.4, 171.0, 170.0, 170.7)
    c4[193] = candle(c4[193][0], 169.8, 170.2, 168.8, 169.2)
    c4[194] = candle(c4[194][0], 168.8, 169.2, 167.8, 168.2)

    c1 = []
    base1 = now - 220 * hour
    for i in range(220):
        p = 160 + 0.01 * i + 1.5 * math.sin(i / 7)
        c1.append(candle(base1 + i * hour, p - 0.1, p + 0.3, p - 0.3, p, 1000))

    c1[215] = candle(base1 + 215 * hour, 158.0, 158.4, 157.2, 157.6, 1000)
    c1[216] = candle(base1 + 216 * hour, 157.5, 157.8, 156.0, 156.6, 2000)
    c1[217] = candle(base1 + 217 * hour, 156.6, 158.7, 156.4, 158.4, 2500)
    decision_now = c1[217][0] + hour
    return daily, c4, c1[:218], decision_now


def test_approved_signal_timeframes_are_exact():
    assert engine.APPROVED_TIMEFRAMES == ("1D", "12H", "4H", "1H")
    assert set(engine.TIMEFRAME_ALIASES) == {"1H", "1HR", "1HOUR", "4H", "4HR", "4HOUR", "12H", "12HR", "12HOUR", "1D", "1DAY"}


def test_v11_long_impulse_requires_hh_hl_structure():
    _, c4, _, _ = _v11_fixture()
    impulses = _v11_find_impulses(engine.convert_candles(c4), "LONG")
    assert impulses
    assert impulses[-1]["structure_label"] == "HH/HL"
    assert impulses[-1]["high"] > impulses[-1]["low"]


def test_v11_short_impulse_requires_lh_ll_structure():
    _, c4, _, _ = _v11_fixture()
    impulses = _v11_find_impulses(engine.convert_candles(_mirror(c4)), "SHORT")
    assert impulses
    assert impulses[-1]["structure_label"] == "LH/LL"


def test_v11_trigger_is_strict_sweep_then_later_reclaim():
    _, _, c1, _ = _v11_fixture()
    result = _v11_liquidity_trigger(engine.convert_candles(c1), 215, "LONG", max_bars=2)
    assert result["ready"] is True
    assert result["reclaim_idx"] == 217
    assert c1[216][4] < result["swept_level"]
    assert c1[217][4] > result["swept_level"]


def test_v11_trigger_is_symmetric_for_shorts():
    _, _, c1, _ = _v11_fixture()
    mirrored = engine.convert_candles(_mirror(c1))
    result = _v11_liquidity_trigger(mirrored, 215, "SHORT", max_bars=2)
    assert result["ready"] is True
    assert result["reclaim_idx"] == 217
    assert mirrored[216][4] > result["swept_level"]
    assert mirrored[217][4] < result["swept_level"]


def test_v11_target_path_is_a_real_diagnostic():
    rows = [candle(1_700_000_000_000 + i * 3_600_000, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(40)]
    result = _v11_target_path(
        engine.convert_candles(rows),
        engine.convert_candles(rows),
        engine.convert_candles(rows),
        100.0,
        140.0,
        "LONG",
    )
    assert set(result) >= {"clear", "obstacles", "reason"}
    assert isinstance(result["obstacles"], list)


def test_v11_analysis_exposes_next_open_market_entry():
    daily, c4, c1, now = _v11_fixture()
    result = engine.analyze_candles("TEST_USDT", daily, None, c4, c1, now_ms=now)
    assert result["setup"] == "LONG"
    assert result["entry_mode"] == "MARKET"
    assert result["entry_time"] == result["candle_time"] == now
    assert result["stop_loss"] < result["entry"] < result["tp"]
    assert result["target_path_clear"] is True
