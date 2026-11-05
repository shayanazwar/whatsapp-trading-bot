from datetime import datetime, timedelta, timezone

import pandas as pd

from rslr.charts import generate_chart


def test_chart_is_created_from_real_frame(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(100):
        price = 100 + i * 0.1
        rows.append({"timestamp": start + timedelta(hours=i), "open": price, "high": price + 1, "low": price - 1, "close": price + .2, "volume": 1000 + i})
    path = generate_chart(pd.DataFrame(rows), "BTC_USDT", "1H", str(tmp_path))
    assert path.exists()
    assert path.stat().st_size > 1000
