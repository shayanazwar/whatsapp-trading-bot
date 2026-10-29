import pytest

from app.charting import ChartError, render_candle_chart
from tests.helpers import candles


@pytest.mark.parametrize("tf,step", [("1H", 3600), ("4H", 14400), ("12H", 43200), ("1D", 86400)])
def test_chart_renders_strategy_timeframes(tmp_path, tf, step):
    bars = candles("BTC_USDT", tf, 1704067200, 250, step, trend=0.01)
    output = render_candle_chart(bars, tmp_path / f"chart_{tf}.png", timeframe=tf)
    assert output.exists()
    assert output.stat().st_size > 10000


def test_chart_refuses_unapproved_timeframe(tmp_path):
    bars = candles("BTC_USDT", "1H", 1704067200, 220, 3600)
    with pytest.raises(ChartError, match="Charts support"):
        render_candle_chart(bars, tmp_path / "chart.png", timeframe="2H")


def test_chart_refuses_mixed_timeframes(tmp_path):
    bars = candles("BTC_USDT", "1H", 1704067200, 220, 3600)
    with pytest.raises(ChartError, match="Every chart candle"):
        render_candle_chart(bars, tmp_path / "chart.png", timeframe="4H")


def test_chart_refuses_too_few_bars(tmp_path):
    bars = candles("BTC_USDT", "1H", 1704067200, 20, 3600)
    with pytest.raises(ChartError, match="At least 30"):
        render_candle_chart(bars, tmp_path / "chart.png")
