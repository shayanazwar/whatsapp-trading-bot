from __future__ import annotations

from app.analysis.engine import analyze_candles, synthesize_12h_from_4h, _v11_find_impulses, _v11_liquidity_trigger, _v11_target_path

DAY = 86_400_000
HOUR = 3_600_000
FOUR_HOUR = 14_400_000


def _build_fixture():
    now = (1_760_000_000_000 // HOUR) * HOUR

    # 1D bullish macro regime with confirmed HH/HL structure.
    daily = []
    base = now - 250 * DAY
    for i in range(250):
        p = 100 + 0.25 * i + 4 * __import__("math").sin(i / 6)
        daily.append({"time": base + i * DAY, "open": p - 0.2, "high": p + 0.8, "low": p - 0.8, "close": p, "volume": 1000})

    # 4H: completed bullish impulse followed by a controlled pullback.
    c4 = []
    base4 = ((now // FOUR_HOUR) - 220) * FOUR_HOUR
    for i in range(220):
        if i < 160:
            p = 130 + 0.08 * i + 2 * __import__("math").sin(i / 5)
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
        c4.append({"time": base4 + i * FOUR_HOUR, "open": p - 0.2, "high": p + 0.7, "low": p - 0.7, "close": p, "volume": 1000})

    # Move the final 4H impulse high one bar later so the active setup is
    # exactly inside V11's 30-bar lifetime at the 1H trigger.
    c4[192] = {"time": c4[192]["time"], "open": 170.4, "high": 171.0, "low": 170.0, "close": 170.7, "volume": 1000}
    c4[193] = {"time": c4[193]["time"], "open": 169.8, "high": 170.2, "low": 168.8, "close": 169.2, "volume": 1000}
    c4[194] = {"time": c4[194]["time"], "open": 168.8, "high": 169.2, "low": 167.8, "close": 168.2, "volume": 1000}

    # 1H: value touch → sweep → reclaim. Index 217 is the completed trigger.
    c1 = []
    base1 = now - 220 * HOUR
    for i in range(220):
        p = 160 + 0.01 * i + 1.5 * __import__("math").sin(i / 7)
        c1.append({"time": base1 + i * HOUR, "open": p - 0.1, "high": p + 0.3, "low": p - 0.3, "close": p, "volume": 1000})
    vals = [168, 166, 164, 162, 160, 158, 157.8, 157.2, 156.8, 158.8, 159.5, 160.0]
    start = 220 - len(vals)
    for k, p in enumerate(vals):
        i = start + k
        c1[i] = {
            "time": base1 + i * HOUR,
            "open": p - 0.4 if k >= 9 else p - 0.1,
            "high": max(p - 0.4 if k >= 9 else p - 0.1, p) + 0.4,
            "low": min(p - 0.4 if k >= 9 else p - 0.1, p) - 0.4,
            "close": p,
            "volume": 2000 if k >= 8 else 1000,
        }

    # Strict V11 sequence: sweep candle breaches prior liquidity, then the
    # subsequent 1H candle alone performs the reclaim. The sweep candle may
    # close on either side of the level; it is the later reclaim that confirms.
    c1[215] = {"time": base1 + 215 * HOUR, "open": 158.0, "high": 158.4, "low": 157.2, "close": 157.6, "volume": 1000}
    sweep_idx = 216
    c1[sweep_idx] = {"time": base1 + sweep_idx * HOUR, "open": 157.5, "high": 157.8, "low": 156.0, "close": 157.4, "volume": 2000}
    trigger_idx = 217
    c1[trigger_idx] = {"time": base1 + trigger_idx * HOUR, "open": 156.6, "high": 158.7, "low": 156.4, "close": 158.4, "volume": 2500}
    decision_now = c1[trigger_idx]["time"] + HOUR
    return daily, c4, c1[: trigger_idx + 1], decision_now


def test_v11_generates_causal_value_pullback_signal():
    daily, c4, c1, now = _build_fixture()
    result = analyze_candles("TEST_USDT", daily, None, c4, c1, now_ms=now)

    assert result["signal_engine_version"].startswith("V11-")
    assert result["setup"] == "LONG"
    assert result["technical_candidate"] is True
    assert result["trigger_type"] == "LIQUIDITY_SWEEP_RECLAIM"
    assert result["entry_mode"] == "MARKET"
    assert result["primary_entry_timeframe"] == "1H"
    assert result["signal_candle_timeframe"] == "1H"
    assert result["rr"] >= 1.60
    assert result["stop_loss"] < result["entry"] < result["tp"]
    assert result["value_zone_low"] < result["value_zone_high"]
    assert result["swept_level_1h"] < result["entry"]


def test_v11_12h_is_causally_synthesized():
    _, c4, _, now = _build_fixture()
    c12 = synthesize_12h_from_4h(c4, now_ms=now)
    assert len(c12) >= 60
    for i in range(1, len(c12)):
        assert c12[i]["time"] - c12[i - 1]["time"] == 12 * HOUR


def test_v11_strict_sweep_requires_later_reclaim():
    _, _, c1, _ = _build_fixture()
    out = _v11_liquidity_trigger(c1, 216, "LONG", max_bars=2)
    assert out["ready"] is True
    assert out["sweep_idx"] == 216
    assert out["reclaim_idx"] == 217
    sweep = c1[216]
    assert sweep["low"] < out["swept_level"]
    assert c1[217]["close"] > out["swept_level"]


def test_v11_impulse_requires_real_hh_hl_and_short_lh_ll():
    _, c4, _, _ = _build_fixture()
    longs = _v11_find_impulses(c4, "LONG")
    assert longs
    assert longs[-1]["structure_label"] == "HH/HL"
    mirror = []
    for c in c4:
        k = 400.0
        mirror.append({"time": c["time"], "open": k - c["open"], "high": k - c["low"], "low": k - c["high"], "close": k - c["close"], "volume": c["volume"]})
    shorts = _v11_find_impulses(mirror, "SHORT")
    assert shorts
    assert shorts[-1]["structure_label"] == "LH/LL"


def test_v11_target_path_reports_obstacle_truthfully():
    c = [
        {"time": i * 1_000, "open": 100+i, "high": 101+i, "low": 99+i, "close": 100.5+i, "volume": 1_000}
        for i in range(20)
    ]
    result = _v11_target_path(c, c, c, 100.0, 115.0, "LONG")
    assert "clear" in result and "obstacles" in result and "reason" in result


def test_v11_short_analysis_is_supported_symmetrically():
    daily, c4, c1, now = _build_fixture()
    # Mirror around the actual LONG entry so RR/cost geometry remains scale-symmetric.
    k = 316.8
    daily_s = [{"time": x["time"], "open": k-x["open"], "high": k-x["low"], "low": k-x["high"], "close": k-x["close"], "volume": x["volume"]} for x in daily]
    c4_s = [{"time": x["time"], "open": k-x["open"], "high": k-x["low"], "low": k-x["high"], "close": k-x["close"], "volume": x["volume"]} for x in c4]
    c1_s = [{"time": x["time"], "open": k-x["open"], "high": k-x["low"], "low": k-x["high"], "close": k-x["close"], "volume": x["volume"]} for x in c1]
    result = analyze_candles("SHORT_TEST_USDT", daily_s, None, c4_s, c1_s, now_ms=now)
    assert result["setup"] == "SHORT"
    assert result["technical_candidate"] is True
    assert result["structure_4h"] == "LH/LL"
    assert result["stop_loss"] > result["entry"] > result["tp"]


def test_v11_liquidity_trigger_accepts_wick_sweep_before_later_reclaim():
    _, _, c1, _ = _build_fixture()
    c1 = list(c1)
    # Make the sweep candle close back above the liquidity level while still
    # requiring the next candle to be the actual reclaim confirmation.
    c1[216] = {"time": c1[216]["time"], "open": 157.5, "high": 158.0, "low": 156.0, "close": 157.4, "volume": 2000}
    out = _v11_liquidity_trigger(c1, 216, "LONG", max_bars=2)
    assert out["ready"] is True
    assert out["sweep_idx"] == 216
    assert out["reclaim_idx"] == 217
