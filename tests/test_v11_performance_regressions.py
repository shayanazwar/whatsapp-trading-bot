from __future__ import annotations

from runpy import run_path

from app.analysis import engine


def build_fixture():
    return run_path("tests/test_v11_strategy.py")["_build_fixture"]()


def _normalized_closed_frames(daily_raw, c4_raw, c1_raw, decision_ms):
    daily = engine.closed_candle_rows(engine.convert_candles(daily_raw), "1D", decision_ms)
    c4 = engine.closed_candle_rows(engine.convert_candles(c4_raw), "4H", decision_ms)
    c12 = engine.synthesize_12h_from_4h(c4, now_ms=decision_ms)
    c1 = engine.closed_candle_rows(engine.convert_candles(c1_raw), "1H", decision_ms)
    return daily, c12, c4, c1


def test_preclosed_fast_path_matches_normal_causal_engine_output():
    daily_raw, c4_raw, c1_raw, decision_ms = build_fixture()
    reference = engine.analyze_candles(
        "TEST_USDT", daily_raw, None, c4_raw, c1_raw, now_ms=decision_ms
    )
    daily, c12, c4, c1 = _normalized_closed_frames(
        daily_raw, c4_raw, c1_raw, decision_ms
    )
    fast = engine.analyze_candles(
        "TEST_USDT", daily, c12, c4, c1,
        now_ms=decision_ms,
        cache={"_preclosed_candles": True},
    )
    assert fast == reference


def test_current_only_sweep_search_matches_full_search_for_current_reclaim():
    _, _, c1_raw, _ = build_fixture()
    c1 = engine.convert_candles(c1_raw)
    full = engine._v11_liquidity_trigger(c1, 216, "LONG", max_bars=6)
    fast = engine._v11_liquidity_trigger(
        c1, 216, "LONG", max_bars=6, current_only=True
    )
    assert full == fast
    assert fast["ready"] is True
    assert fast["reclaim_idx"] == len(c1) - 1


def test_current_only_ignores_historical_reclaim_that_analyzer_would_reject():
    _, _, c1_raw, _ = build_fixture()
    c1 = list(engine.convert_candles(c1_raw))
    last = c1[-1]
    c1.append(engine.Candle(
        time=int(last["time"]) + 3_600_000,
        open=158.4,
        high=158.5,
        low=156.0,
        close=156.5,
        volume=1000.0,
    ))
    full = engine._v11_liquidity_trigger(c1, 216, "LONG", max_bars=6)
    fast = engine._v11_liquidity_trigger(
        c1, 216, "LONG", max_bars=6, current_only=True
    )
    assert full["ready"] is True
    assert full["reclaim_idx"] < len(c1) - 1
    assert fast["ready"] is False


def test_no_signal_diagnostics_match_with_and_without_reuse_cache():
    daily_raw, c4_raw, c1_raw, decision_ms = build_fixture()
    daily, c12, c4, c1 = _normalized_closed_frames(
        daily_raw, c4_raw, c1_raw, decision_ms
    )
    daily_state = engine._v11_regime_1d(daily)
    reference = engine._v11_empty(
        "TEST_USDT", daily, c12, c4, c1, "test rejection", daily_state
    )
    cache = {"_preclosed_candles": True}
    first = engine._v11_empty(
        "TEST_USDT", daily, c12, c4, c1, "test rejection", daily_state, cache=cache
    )
    second = engine._v11_empty(
        "TEST_USDT", daily, c12, c4, c1, "test rejection", daily_state, cache=cache
    )
    assert first == reference
    assert second == reference
