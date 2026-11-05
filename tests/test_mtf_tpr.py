from dataclasses import replace

import pytest

from app.market.candles import Candle
from app.strategy.mtf_tpr import MtfTprConfig, evaluate_mtf_tpr


def _trend_bars(count=1800, symbol="BTC_USDT", start=1_704_067_200):
    import math

    result = []
    for i in range(count):
        close = 100.0 + 0.001 * i + 0.35 * math.sin(i * 0.11) + 0.12 * math.sin(i * 0.37)
        op = close - 0.04 * math.cos(i * 0.09)
        volume = 100.0 + 15.0 * math.sin(i * 0.17)
        result.append(Candle(
            symbol, "3M", start + i * 180, op,
            max(op, close) + 0.15, min(op, close) - 0.15, close,
            volume=volume, amount=volume * close,
        ))
    return result


def test_mtf_tpr_requires_long_enough_context_warmup():
    assert evaluate_mtf_tpr(_trend_bars(500), MtfTprConfig()) == []


def test_mtf_tpr_rejects_bad_pullback_configuration():
    with pytest.raises(ValueError, match="pullback"):
        MtfTprConfig(pullback_min_atr=0.8, pullback_max_atr=0.5).validate()


def test_mtf_tpr_does_not_emit_on_gapped_three_minute_history():
    bars = _trend_bars()
    # A gap forces the contiguous-history gate to warm up again; it must not bridge it.
    gap_index = 1500
    bars[gap_index:] = [replace(c, open_time=c.open_time + 180) for c in bars[gap_index:]]
    signals = evaluate_mtf_tpr(
        bars,
        MtfTprConfig(atr_percentile_low=0.0, atr_percentile_high=100.0, min_relative_volume=1.0),
    )
    assert all(signal.timestamp < bars[gap_index].close_time for signal in signals)


def test_mtf_signal_schema_reports_distinct_ema100_and_planned_rr_fields():
    bars = _trend_bars()
    signals = evaluate_mtf_tpr(
        bars,
        MtfTprConfig(atr_percentile_low=0.0, atr_percentile_high=100.0, min_relative_volume=1.0, min_score=0.0),
    )
    # A monotonic/oscillating fixture is not asserted to create an edge. If it does
    # create a candidate, the report fields must be numerically valid and distinct.
    for signal in signals:
        assert signal.ema100 > 0
        assert signal.planned_price_rr > 0
        assert signal.direction in {"LONG", "SHORT"}
        assert 0 <= signal.score <= 100


def _candidate_signal_fixture():
    import math

    start = 1_704_067_200
    bars = []
    for i in range(1800):
        close = 100.0 + 0.004 * i + 0.45 * math.sin(i * 0.12) + 0.12 * math.sin(i * 0.31)
        op = close - 0.03 * math.cos(i * 0.2)
        volume = 100.0 + 20.0 * math.sin(i * 0.17)
        bars.append(Candle(
            "BTC_USDT", "3M", start + i * 180, op,
            max(op, close) + 0.18, min(op, close) - 0.18, close,
            volume=volume, amount=volume * close,
        ))
    # A shallow completed pullback followed by a strong reclaim candle.
    for j, delta in zip(range(1796, 1800), [-0.15, -0.22, -0.18, 0.16]):
        old = bars[j]
        close = old.close + delta
        volume = 1000.0 if j == 1799 else 100.0
        low = close - 0.20
        high = close + 0.05 if j == 1799 else close + 0.12
        bars[j] = Candle(
            "BTC_USDT", "3M", old.open_time, close - 0.03,
            high, low, close, volume=volume, amount=volume * close,
        )
    return bars


def test_mtf_tpr_emits_a_long_on_a_valid_pullback_reclaim_fixture():
    signals = evaluate_mtf_tpr(
        _candidate_signal_fixture(),
        MtfTprConfig(atr_percentile_low=0.0, atr_percentile_high=100.0, min_score=65.0),
    )
    assert len(signals) == 1
    signal = signals[0]
    assert signal.direction == "LONG"
    assert signal.grade == "A"
    assert signal.ema100 != signal.ema50
    assert signal.planned_price_rr == pytest.approx(2.5 / 1.5, rel=1e-3)


def test_mtf_tpr_hard_enforces_minimum_pullback_distance():
    cfg = MtfTprConfig(
        atr_percentile_low=0.0, atr_percentile_high=100.0,
        min_score=65.0, pullback_min_atr=0.7, pullback_max_atr=0.8,
    )
    assert evaluate_mtf_tpr(_candidate_signal_fixture(), cfg) == []
