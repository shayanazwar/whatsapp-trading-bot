from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from rslr.config import settings
from rslr.exchange.mexc import MEXCRestClient
from rslr.storage import Database


def reconcile_paper_positions(
    client: MEXCRestClient | None = None,
    database: Database | None = None,
) -> int:
    """Resolve stored signal observations from subsequent completed 1H candles.

    If stop and target are both touched in the same candle, SL is assumed first.
    Open positions at the end of available data remain OPEN.
    """
    owned = client is None
    client = client or MEXCRestClient()
    database = database or Database()
    resolved = 0
    try:
        now = datetime.now(timezone.utc)
        for position in database.open_paper_positions():
            opened = position["opened_at"]
            start = opened + timedelta(hours=1)  # entry signal was based on the prior completed candle
            try:
                frame = client.get_klines(position["symbol"], start, now, "Min60")
            except Exception:
                continue
            for _, bar in frame.iterrows():
                timestamp = pd.Timestamp(bar.timestamp).to_pydatetime()
                opened_price = float(bar.open)
                high = float(bar.high)
                low = float(bar.low)
                side = position["side"]
                stop = float(position["stop_loss"])
                target = float(position["take_profit"])
                result = None
                exit_price = 0.0
                if side == "LONG":
                    if opened_price <= stop:
                        result, exit_price = "SL", opened_price
                    elif low <= stop:
                        result, exit_price = "SL", stop
                    elif high >= target:
                        result, exit_price = "TP", target
                else:
                    if opened_price >= stop:
                        result, exit_price = "SL", opened_price
                    elif high >= stop:
                        result, exit_price = "SL", stop
                    elif low <= target:
                        result, exit_price = "TP", target
                if result:
                    database.close_paper_position(position["id"], result, exit_price, timestamp, 0.0)
                    resolved += 1
                    break
                if timestamp >= opened + timedelta(hours=settings.max_hold_hours):
                    database.close_paper_position(position["id"], "EXPIRED", float(bar.close), timestamp, 0.0)
                    resolved += 1
                    break
        return resolved
    finally:
        if owned:
            client.close()
