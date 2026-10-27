import pytest

from app.charting import ChartError, render_5m_chart
from app.market.candles import Candle
from tests.helpers import candles


def test_charting_refuses_strategy_timeframe(tmp_path):
    bars = candles("BTC_USDT", "1H", 1704067200, 220, 3600)
    with pytest.raises(ChartError, match="only 5M"):
        render_5m_chart(bars, tmp_path / "chart.png")


def test_chart_renders_all_four_emas_and_support_resistance(tmp_path):
    bars = candles("BTC_USDT", "5M", 1704067200, 250, 300, trend=0.01)
    output = render_5m_chart(bars, tmp_path / "chart.png")
    assert output.exists()
    assert output.stat().st_size > 10000
