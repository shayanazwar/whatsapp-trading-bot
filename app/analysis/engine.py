from typing import Dict, List

from .indicators import ema, rsi, volume_status
from .structure import (
    get_structure,
    get_trend,
    get_bos,
    get_support_resistance,
)


def convert_candles(rows: List) -> List[Dict]:
    return [
        {
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in rows
    ]


async def analyze_symbol(market, symbol: str) -> Dict:

    ref = await market.resolve(symbol)

    candles_4h = convert_candles(
        await market.ohlcv(ref, "4H", 200)
    )

    candles_1h = convert_candles(
        await market.ohlcv(ref, "1H", 200)
    )

    candles_15m = convert_candles(
        await market.ohlcv(ref, "15M", 200)
    )

    closes_15m = [
        candle["close"]
        for candle in candles_15m
    ]

    ema21 = ema(closes_15m, 21)
    ema50 = ema(closes_15m, 50)

    current_price = closes_15m[-1]

    rsi_value = rsi(closes_15m)

    trend_4h = get_trend(candles_4h)

    structure_1h = get_structure(candles_1h)

    bos_15m = get_bos(candles_15m)

    support, resistance = get_support_resistance(
        candles_15m
    )

    volume = volume_status(candles_15m)

    if ema21 > ema50:
        ema_direction = "BULLISH"
    elif ema21 < ema50:
        ema_direction = "BEARISH"
    else:
        ema_direction = "NEUTRAL"

    return {
        "symbol": ref.symbol,
        "price": current_price,
        "trend_4h": trend_4h,
        "structure_1h": structure_1h,
        "bos_15m": bos_15m,
        "ema21": ema21,
        "ema50": ema50,
        "ema_direction": ema_direction,
        "rsi": rsi_value,
        "volume": volume,
        "support": support,
        "resistance": resistance,
    }
