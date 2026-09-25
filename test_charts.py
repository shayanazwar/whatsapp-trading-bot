import asyncio
from pathlib import Path

from app.charts import ChartRenderer
from app.market import MarketRef


def test_chart_render(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rows = []
    base = 1700000000000
    price = 100.0
    for i in range(80):
        open_p = price
        close = price + (1 if i % 2 == 0 else -0.5)
        high = max(open_p, close) + 1
        low = min(open_p, close) - 1
        rows.append([base + i * 300000, open_p, high, low, close, 1000])
        price = close
    path = asyncio.run(ChartRenderer().render(MarketRef("binance", "TESTUSDT"), "5m", rows))
    assert path.exists()
    assert path.stat().st_size > 1000
    path.unlink(missing_ok=True)
