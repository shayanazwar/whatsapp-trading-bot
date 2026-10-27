from __future__ import annotations

from app.market.candles import Candle


def candles(symbol: str, timeframe: str, start: int, count: int, step: int, *, base: float = 100.0, trend: float = 0.0, complete: bool = True):
    result = []
    for i in range(count):
        close = base + trend * i
        op = close - trend * 0.25
        high = max(op, close) + 0.25
        low = min(op, close) - 0.25
        result.append(Candle(symbol, timeframe, start + i * step, op, high, low, close, volume=100+i, amount=(100+i)*close, complete=complete))
    return result
