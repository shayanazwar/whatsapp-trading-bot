from __future__ import annotations

from app.analysis.patterns import confirm_triangle_breakout, detect_triangle_pattern

HOUR = 3_600_000
FOUR_HOURS = 4 * HOUR


def candle(ts, open_, high, low, close, volume=1_000.0):
    return {
        "time": ts,
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
    }


def converging_triangle(base=1_760_000_000_000):
    rows = []
    for i in range(56):
        upper = 120.0 - 0.30 * i
        lower = 80.0 + 0.30 * i
        middle = (upper + lower) / 2.0
        open_ = middle - 0.1
        close = middle + 0.1
        high = middle + 1.8
        low = middle - 1.8
        if i % 8 == 4:
            open_, close, high, low = upper - 2.0, upper - 1.5, upper, upper - 3.0
        if i % 8 == 0:
            open_, close, low, high = lower + 2.0, lower + 1.5, lower, lower + 3.0
        rows.append(candle(base + i * FOUR_HOURS, open_, high, low, close))
    return rows


def test_detects_confirmed_symmetrical_triangle_without_future_pivots():
    rows = converging_triangle()
    pattern = detect_triangle_pattern(rows, atr_4h=3.0)
    assert pattern is not None
    assert pattern["kind"] == "SYMMETRICAL_TRIANGLE"
    assert pattern["upper_slope"] < 0 < pattern["lower_slope"]
    assert pattern["current_width"] < pattern["start_width"]
    # Appending a single candle cannot retroactively alter prior confirmed pivots.
    assert pattern["end_idx"] <= len(rows) - 3


def test_confirms_long_breakout_only_after_closed_1h_candle():
    base = 1_760_000_000_000
    c4 = converging_triangle(base)
    pattern = detect_triangle_pattern(c4, atr_4h=3.0)
    assert pattern is not None

    rows = []
    start_time = base + 54 * FOUR_HOURS
    for i in range(24):
        ts = start_time + i * HOUR
        x = (ts - base) / FOUR_HOURS
        boundary = pattern["upper_slope"] * x + pattern["upper_intercept"]
        close = boundary - 1.0
        rows.append(candle(ts, close - 0.1, close + 0.2, close - 0.3, close, 1_000.0))
    ts = rows[-1]["time"] + HOUR
    x = (ts - base) / FOUR_HOURS
    boundary = pattern["upper_slope"] * x + pattern["upper_intercept"]
    rows.append(candle(ts, boundary - 0.25, boundary + 0.40, boundary - 0.35, boundary + 0.25, 1_400.0))

    result = confirm_triangle_breakout(rows, c4, pattern, "LONG", atr_1h=1.0)
    assert result is not None
    assert result["ready"] is True
    assert result["trigger_type"] == "TRIANGLE_BREAKOUT"
    assert result["boundary"] < rows[-1]["close"]


def test_confirms_short_breakout_symmetrically():
    base = 1_760_000_000_000
    c4 = converging_triangle(base)
    pattern = detect_triangle_pattern(c4, atr_4h=3.0)
    assert pattern is not None

    rows = []
    start_time = base + 54 * FOUR_HOURS
    for i in range(24):
        ts = start_time + i * HOUR
        x = (ts - base) / FOUR_HOURS
        boundary = pattern["lower_slope"] * x + pattern["lower_intercept"]
        close = boundary + 1.0
        rows.append(candle(ts, close + 0.1, close + 0.3, close - 0.2, close, 1_000.0))
    ts = rows[-1]["time"] + HOUR
    x = (ts - base) / FOUR_HOURS
    boundary = pattern["lower_slope"] * x + pattern["lower_intercept"]
    rows.append(candle(ts, boundary + 0.25, boundary + 0.35, boundary - 0.40, boundary - 0.25, 1_400.0))

    result = confirm_triangle_breakout(rows, c4, pattern, "SHORT", atr_1h=1.0)
    assert result is not None
    assert result["ready"] is True
    assert result["trigger_type"] == "TRIANGLE_BREAKOUT"
    assert result["boundary"] > rows[-1]["close"]


def _sloped_triangle(upper_slope: float, lower_slope: float, upper_start: float = 110.0, lower_start: float = 90.0):
    rows = []
    base = 1_760_000_000_000
    for i in range(56):
        upper = upper_start + upper_slope * i
        lower = lower_start + lower_slope * i
        middle = (upper + lower) / 2.0
        span = upper - lower
        open_ = middle - 0.02
        close = middle + 0.02
        high = min(upper - 0.01, middle + span * 0.10)
        low = max(lower + 0.01, middle - span * 0.10)
        if i % 8 == 4:
            open_, close, high, low = upper - 0.20, upper - 0.10, upper, max(lower + 0.01, upper - 0.30)
        if i % 8 == 0:
            open_, close, high, low = lower + 0.20, lower + 0.10, min(upper - 0.01, lower + 0.30), lower
        rows.append(candle(base + i * FOUR_HOURS, open_, high, low, close))
    return rows


def test_detects_ascending_and_descending_triangles():
    ascending = detect_triangle_pattern(_sloped_triangle(0.0, 0.25), atr_4h=3.0)
    descending = detect_triangle_pattern(_sloped_triangle(-0.25, 0.0), atr_4h=3.0)
    assert ascending is not None and ascending["kind"] == "ASCENDING_TRIANGLE"
    assert descending is not None and descending["kind"] == "DESCENDING_TRIANGLE"


def test_rejects_parallel_channel_and_bad_atr():
    rows = converging_triangle()
    assert detect_triangle_pattern(rows, atr_4h=0.0) is None
    # A real parallel-channel check with a valid ATR. The former test passed only
    # because atr=0 short-circuited before any channel geometry was inspected.
    parallel = detect_triangle_pattern(_sloped_triangle(-0.10, -0.10), atr_4h=3.0)
    assert parallel is None


def test_engine_builds_pattern_candidate_with_measured_move_levels(monkeypatch):
    import app.analysis.engine as engine

    base = 1_760_000_000_000
    c4 = engine.convert_candles(converging_triangle(base))
    pattern = detect_triangle_pattern(c4, engine._safe_atr(c4))
    assert pattern is not None

    c1_rows = []
    start_time = base + 50 * FOUR_HOURS
    for i in range(24):
        ts = start_time + i * HOUR
        x = (ts - base) / FOUR_HOURS
        boundary = pattern["upper_slope"] * x + pattern["upper_intercept"]
        close = boundary - 1.0
        c1_rows.append(candle(ts, close - 0.1, close + 0.2, close - 0.3, close, 1_000.0))
    ts = c1_rows[-1]["time"]
    x = (ts - base) / FOUR_HOURS
    boundary = pattern["upper_slope"] * x + pattern["upper_intercept"]
    c1_rows[-1] = candle(ts, boundary - 0.25, boundary + 0.40, boundary - 0.35, boundary + 0.25, 1_400.0)
    c1 = engine.convert_candles(c1_rows)

    monkeypatch.setattr(engine, "_v11_context_12h", lambda *_args: {
        "status": "HEALTHY", "hostile": False, "healthy": True,
        "structure": "RANGE", "e21": 100.0, "e50": 99.0, "slope": 0.01,
    })
    monkeypatch.setattr(engine, "_v11_target_path", lambda *_args, **_kwargs: {
        "clear": True, "obstacles": [], "reason": "test path clear",
    })
    monkeypatch.setattr(engine, "btc_filter_ok", lambda *_args, **_kwargs: (True, "test BTC pass"))

    result = engine._triangle_analyze_side(
        "TEST_USDT", "LONG", [], [], c4, c1,
        {"regime": "BULLISH", "bull": True, "bear": False, "bull_votes": 4, "bear_votes": 1},
        0.0015, {"ok": False},
    )
    assert result["candidate"] is True, result.get("reason")
    assert result["setup_type"] == "TRIANGLE_BREAKOUT"
    assert result["target"]["timeframe"] == "MEASURED_MOVE"
    assert result["stop_loss"] < result["entry"] < result["tp"]
    assert result["tp"] == result["pattern_breakout_level"] + result["pattern_height"]


def test_public_engine_defaults_to_triangle_breakout_and_passes_strategy_gates(monkeypatch):
    import app.analysis.engine as engine
    from app.automation.setup_filter import validate_analysis

    monkeypatch.setattr(engine, "SETUP_ARCHITECTURE", "TRIANGLE_BREAKOUT")
    base = 1_800_000_000_000
    now = base + 224 * HOUR
    c4_raw = converging_triangle(base)
    c4 = engine.convert_candles(c4_raw)
    pattern = detect_triangle_pattern(c4, engine._safe_atr(c4))
    assert pattern is not None

    # Align the decision candle with the final 4H formation. Recent 1H closes
    # hover just below projected resistance, then the last completed candle breaks it.
    c1_raw = []
    start_1h = base + 4 * HOUR
    for index in range(220):
        ts = start_1h + index * HOUR
        if index >= 196:
            x = (ts - base) / FOUR_HOURS
            boundary = pattern["upper_slope"] * x + pattern["upper_intercept"]
            close = boundary - 1.0
            candle_row = candle(ts, close - 0.1, close + 0.2, close - 0.3, close, 1_000.0)
        else:
            candle_row = candle(ts, 100.0, 100.5, 99.5, 100.0, 1_000.0)
        c1_raw.append(candle_row)
    last_ts = c1_raw[-1]["time"]
    x = (last_ts - base) / FOUR_HOURS
    boundary = pattern["upper_slope"] * x + pattern["upper_intercept"]
    c1_raw[-1] = candle(
        last_ts, boundary - 0.25, boundary + 0.40, boundary - 0.35,
        boundary + 0.25, 1_400.0,
    )

    day = 24 * HOUR
    c1d = engine.convert_candles([
        candle(now - 220 * day + i * day, 100.0, 101.0, 99.0, 100.0 + i * 0.01, 1_000.0)
        for i in range(220)
    ])
    c12 = engine.convert_candles([
        candle(now - 70 * 12 * HOUR + i * 12 * HOUR, 100.0, 101.0, 99.0, 100.0, 1_000.0)
        for i in range(70)
    ])
    c1 = engine.convert_candles(c1_raw)

    monkeypatch.setattr(engine, "_data_quality", lambda *_args: (True, "OK"))
    monkeypatch.setattr(engine, "_v11_regime_1d", lambda _c: {
        "regime": "BULLISH", "bull": True, "bear": False,
        "e21": 101.0, "e50": 100.0, "e200": 99.0,
        "slope": 0.01, "adx": 20.0, "structure": "HH/HL",
        "price": 102.0, "bull_votes": 5, "bear_votes": 1,
    })
    monkeypatch.setattr(engine, "_v11_context_12h", lambda *_args: {
        "status": "HEALTHY", "hostile": False, "healthy": True,
        "structure": "RANGE", "e21": 100.0, "e50": 99.0, "slope": 0.01,
    })
    monkeypatch.setattr(engine, "_v11_target_path", lambda *_args, **_kwargs: {
        "clear": True, "obstacles": [], "reason": "test path clear",
    })
    monkeypatch.setattr(engine, "btc_filter_ok", lambda *_args, **_kwargs: (True, "test BTC pass"))
    monkeypatch.setattr(engine, "_atr_percentile", lambda *_args: 50.0)

    result = engine.analyze_candles(
        "TEST_USDT", c1d, c12, c4, c1,
        now_ms=now, btc_context={"ok": True}, allow_shorts=True,
    )

    assert result["setup"] == "LONG", result.get("diagnostic_failures")
    assert result["setup_type"] == "TRIANGLE_BREAKOUT"
    assert result["engine_config"]["stop_loss_mode"] == "OPPOSITE_TRIANGLE_BOUNDARY"
    assert result["trigger_type"] in {"TRIANGLE_BREAKOUT", "TRIANGLE_BREAKOUT_RETEST"}
    assert result["tp"] == result["pattern_breakout_level"] + result["pattern_height"]
    assert result["stop_loss"] < result["entry"] < result["tp"]

    # The scanner supplies these two fields after ticker/order-book preflight.
    result["data_fresh"] = True
    result["futures_execution_ok"] = True
    result["mexc_spread_pct"] = 0.01
    result["max_allowed_spread_pct"] = 0.10
    accepted, reasons = validate_analysis(result, min_confluence=0, min_rr=1.6)
    assert accepted, reasons


def test_detects_shallow_triangle_boundaries_normalized_to_atr():
    """Ordinary shallow triangle lines must not be classified as unsupported."""
    base = 1_760_000_000_000
    rows = []
    for i in range(56):
        upper = 103.5 - 0.055 * i
        lower = 96.5 + 0.055 * i
        middle = (upper + lower) / 2.0
        span = upper - lower
        open_ = middle - 0.02
        close = middle + 0.02
        high = min(upper - 0.01, middle + span * 0.10)
        low = max(lower + 0.01, middle - span * 0.10)
        if i % 8 == 4:
            open_, close, high, low = upper - 0.20, upper - 0.10, upper, max(lower + 0.01, upper - 0.30)
        if i % 8 == 0:
            open_, close, high, low = lower + 0.20, lower + 0.10, min(upper - 0.01, lower + 0.30), lower
        rows.append(candle(base + i * FOUR_HOURS, open_, high, low, close))

    diagnostics = {}
    pattern = detect_triangle_pattern(rows, atr_4h=3.0, diagnostics=diagnostics)
    assert pattern is not None, diagnostics
    assert pattern["kind"] == "SYMMETRICAL_TRIANGLE"
    assert 0 < pattern["upper_slope"] * -1 < 0.05 * 3.0
    assert 0 < pattern["lower_slope"] < 0.05 * 3.0


def test_triangle_breakout_can_trade_against_daily_bias_when_12h_is_not_hostile(monkeypatch):
    """A confirmed SHORT breakout must not be discarded only because 1D is bullish."""
    import app.analysis.engine as engine

    base = 1_760_000_000_000
    c4 = engine.convert_candles(converging_triangle(base))
    pattern = detect_triangle_pattern(c4, atr_4h=3.0)
    assert pattern is not None

    c1_rows = []
    start_time = base + 50 * FOUR_HOURS
    for i in range(24):
        ts = start_time + i * HOUR
        x = (ts - base) / FOUR_HOURS
        boundary = pattern["lower_slope"] * x + pattern["lower_intercept"]
        close = boundary + 1.0
        c1_rows.append(candle(ts, close + 0.1, close + 0.3, close - 0.2, close, 1_000.0))
    ts = c1_rows[-1]["time"]
    x = (ts - base) / FOUR_HOURS
    boundary = pattern["lower_slope"] * x + pattern["lower_intercept"]
    c1_rows[-1] = candle(ts, boundary + 0.25, boundary + 0.35, boundary - 0.40, boundary - 0.25, 1_400.0)
    c1 = engine.convert_candles(c1_rows)

    monkeypatch.setattr(engine, "_v11_context_12h", lambda *_args: {
        "status": "HEALTHY", "hostile": False, "healthy": True,
        "structure": "RANGE", "e21": 100.0, "e50": 99.0, "slope": 0.01,
    })
    monkeypatch.setattr(engine, "_v11_target_path", lambda *_args, **_kwargs: {
        "clear": True, "obstacles": [], "reason": "test path clear",
    })
    monkeypatch.setattr(engine, "btc_filter_ok", lambda *_args, **_kwargs: (True, "test BTC pass"))
    monkeypatch.setattr(engine, "_atr_percentile", lambda *_args: 50.0)

    result = engine._triangle_analyze_side(
        "TEST_USDT", "SHORT", [], [], c4, c1,
        {"regime": "BULLISH", "bull": True, "bear": False, "bull_votes": 4, "bear_votes": 1},
        0.0015, {"ok": True},
    )
    assert result["candidate"] is True, result.get("reason")
    assert result["direction_ok"] is False
    assert result["daily_bias_alignment"] == "OPPOSED"
    assert result["tp"] < result["entry"] < result["stop_loss"]


def test_public_engine_emits_countertrend_short_breakout_end_to_end(monkeypatch):
    """The live public engine path must preserve a verified SHORT breakout in a bullish 1D regime."""
    import app.analysis.engine as engine

    monkeypatch.setattr(engine, "SETUP_ARCHITECTURE", "TRIANGLE_BREAKOUT")
    base = 1_800_000_000_000
    now = base + 224 * HOUR
    c4_raw = converging_triangle(base)
    c4 = engine.convert_candles(c4_raw)
    pattern = detect_triangle_pattern(c4, engine._safe_atr(c4))
    assert pattern is not None

    c1_raw = []
    start_1h = base + 4 * HOUR
    for index in range(220):
        ts = start_1h + index * HOUR
        if index >= 196:
            x = (ts - base) / FOUR_HOURS
            boundary = pattern["lower_slope"] * x + pattern["lower_intercept"]
            close = boundary + 1.0
            row = candle(ts, close + 0.1, close + 0.3, close - 0.2, close, 1_000.0)
        else:
            row = candle(ts, 100.0, 100.5, 99.5, 100.0, 1_000.0)
        c1_raw.append(row)
    last_ts = c1_raw[-1]["time"]
    x = (last_ts - base) / FOUR_HOURS
    boundary = pattern["lower_slope"] * x + pattern["lower_intercept"]
    c1_raw[-1] = candle(last_ts, boundary + 0.25, boundary + 0.35, boundary - 0.40, boundary - 0.25, 1_400.0)

    day = 24 * HOUR
    c1d = engine.convert_candles([
        candle(now - 220 * day + i * day, 100.0, 101.0, 99.0, 100.0 + i * 0.01, 1_000.0)
        for i in range(220)
    ])
    c12 = engine.convert_candles([
        candle(now - 70 * 12 * HOUR + i * 12 * HOUR, 100.0, 101.0, 99.0, 100.0, 1_000.0)
        for i in range(70)
    ])
    c1 = engine.convert_candles(c1_raw)

    monkeypatch.setattr(engine, "_data_quality", lambda *_args: (True, "OK"))
    monkeypatch.setattr(engine, "_v11_regime_1d", lambda _c: {
        "regime": "BULLISH", "bull": True, "bear": False,
        "e21": 101.0, "e50": 100.0, "e200": 99.0,
        "slope": 0.01, "adx": 20.0, "structure": "HH/HL",
        "price": 102.0, "bull_votes": 5, "bear_votes": 1,
    })
    monkeypatch.setattr(engine, "_v11_context_12h", lambda *_args: {
        "status": "HEALTHY", "hostile": False, "healthy": True,
        "structure": "RANGE", "e21": 100.0, "e50": 99.0, "slope": 0.01,
    })
    monkeypatch.setattr(engine, "_v11_target_path", lambda *_args, **_kwargs: {
        "clear": True, "obstacles": [], "reason": "test path clear",
    })
    monkeypatch.setattr(engine, "btc_filter_ok", lambda *_args, **_kwargs: (True, "test BTC pass"))
    monkeypatch.setattr(engine, "_atr_percentile", lambda *_args: 50.0)

    result = engine.analyze_candles(
        "TEST_USDT", c1d, c12, c4, c1,
        now_ms=now, btc_context={"ok": True}, allow_shorts=True,
    )
    assert result["setup"] == "SHORT", result.get("diagnostic_failures")
    assert result["setup_type"] == "TRIANGLE_BREAKOUT"
    assert result["regime_1d"] == "BULLISH"
    assert result["daily_bias_alignment"] == "OPPOSED"
    assert result["daily_direction_permission"] is False
    assert result["tp"] < result["entry"] < result["stop_loss"]


def test_detector_recovers_noisy_triangle_from_alternative_confirmed_pivots():
    """A noisy adjacent swing must not hide an otherwise valid converging triangle."""
    import random

    rng = random.Random(23)
    base = 1_760_000_000_000
    rows = []
    for i in range(64):
        upper = 120.0 - 0.24 * i
        lower = 80.0 + 0.24 * i
        if i % 8 in (3, 4):
            close = upper - rng.uniform(2.0, 8.0)
        elif i % 8 in (7, 0):
            close = lower + rng.uniform(2.0, 8.0)
        else:
            close = rng.uniform(lower + 5.0, upper - 5.0)
        open_ = close + rng.uniform(-1.2, 1.2)
        high = max(open_, close) + rng.uniform(0.1, 2.0)
        low = min(open_, close) - rng.uniform(0.1, 2.0)
        if i % 8 == 4:
            high = max(high, upper - rng.uniform(0.0, 0.3))
        if i % 8 == 0:
            low = min(low, lower + rng.uniform(0.0, 0.3))
        rows.append(candle(base + i * FOUR_HOURS, open_, high, low, close, 1_000.0))

    diagnostics = {}
    pattern = detect_triangle_pattern(rows, atr_4h=3.0, diagnostics=diagnostics)
    assert pattern is not None, diagnostics
    assert pattern["kind"] == "SYMMETRICAL_TRIANGLE"
    assert pattern["touch_count"] >= 6
    assert pattern["detector_type"] == "TRIANGLE"
    assert diagnostics["candidate_count"] >= 1
