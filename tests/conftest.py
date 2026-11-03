from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pandas as pd
import pytest

from app.db import Base, engine
from app import models  # noqa: F401


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def candles_4h():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(90):
        ts = start + timedelta(hours=4 * i)
        op = price
        close = price * (1 + (0.001 if i % 4 else -0.0004))
        rows.append({"timestamp": ts, "open": op, "high": max(op, close) * 1.002,
                     "low": min(op, close) * 0.998, "close": close,
                     "volume": 100 + i, "amount": 10000 + i})
        price = close
    return pd.DataFrame(rows)


@pytest.fixture
def candles_1h():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(1400):
        ts = start + timedelta(hours=i)
        op = price
        close = price * (1 + 0.0002 + (0.0007 if i % 9 else -0.0005))
        rows.append({"timestamp": ts, "open": op, "high": max(op, close) * 1.001,
                     "low": min(op, close) * 0.999, "close": close,
                     "volume": 100 + (i % 30), "amount": 10000 + i})
        price = close
    return pd.DataFrame(rows)
