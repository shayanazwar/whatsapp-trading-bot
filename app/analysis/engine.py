from __future__ import annotations

from typing import Dict, List, Optional

from .indicators import atr, ema, rsi, volume_status
from .structure import get_bos, get_structure, get_support_resistance, get_trend


TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "1d": 86_400_000,
}


def convert_candles(rows: List) -> List[Dict]:
    """Convert Binance/MEXC-normalized rows into analysis dictionaries."""
    candles: list[Dict] = []
    for row in rows:
        if len(row) < 6:
            continue
        item: Dict = {
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        if row[0] is not None:
            item["time"] = int(row[0])
        candles.append(item)
    return candles


def closed_candle_rows(rows: List, interval: str, now_ms: Optional[int] = None) -> List:
    """Return only fully closed candles, sorted oldest -> newest.

    Candle timestamps are expected to be the candle open time in milliseconds.
    Rows whose candle end is in the future are excluded to prevent look-ahead.
    """
    if interval not in TIMEFRAME_MS:
        raise ValueError(f"Unsupported interval: {interval}")

    import time

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    interval_ms = TIMEFRAME_MS[interval]

    normalized: list[list] = []
    for row in rows:
        if len(row) < 6:
            continue
        timestamp = int(row[0])
        if timestamp < 10**12:
            timestamp *= 1000
        if timestamp + interval_ms > now:
            continue
        normalized.append(
            [
                timestamp,
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            ]
        )

    normalized.sort(key=lambda item: item[0])

    deduped: list[list] = []
    seen: set[int] = set()
    for row in normalized:
        if row[0] in seen:
            continue
        seen.add(row[0])
        deduped.append(row)
    return deduped


def _directional_state(data: Dict) -> tuple[int, int, list[str]]:
    bullish = 0
    bearish = 0
    reasons: list[str] = []

    if data["trend_4h"] == "BULLISH":
        bullish += 1
    elif data["trend_4h"] == "BEARISH":
        bearish += 1

    if data["structure_1h"] == "HH/HL":
        bullish += 1
    elif data["structure_1h"] == "LH/LL":
        bearish += 1

    if data["bos_15m"] == "BULLISH BOS":
        bullish += 1
    elif data["bos_15m"] == "BEARISH BOS":
        bearish += 1

    if data["ema_direction"] == "BULLISH":
        bullish += 1
    elif data["ema_direction"] == "BEARISH":
        bearish += 1

    rsi_value = float(data["rsi"])
    if 50 <= rsi_value <= 70:
        bullish += 1
    elif 30 <= rsi_value < 50:
        bearish += 1

    if bullish >= 4 and bearish == 0:
        reasons = [
            "4H bullish",
            "1H HH/HL",
            "15M bullish BOS",
            "EMA bullish",
            "RSI bullish zone",
        ]
    elif bearish >= 4 and bullish == 0:
        reasons = [
            "4H bearish",
            "1H LH/LL",
            "15M bearish BOS",
            "EMA bearish",
            "RSI bearish zone",
        ]

    return bullish, bearish, reasons


def calculate_confluence(data: Dict) -> Dict:
    bullish_points, bearish_points, aligned_reasons = _directional_state(data)

    if bullish_points >= 4 and bearish_points == 0:
        setup = "LONG"
    elif bearish_points >= 4 and bullish_points == 0:
        setup = "SHORT"
    else:
        setup = "NO TRADE"

    score = max(bullish_points, bearish_points)
    reasons = list(aligned_reasons)

    if data.get("volume") == "INCREASING":
        score += 1
        reasons.append("Volume increasing")

    return {
        "score": score,
        "setup": setup,
        "reasons": reasons,
        "bullish_points": bullish_points,
        "bearish_points": bearish_points,
    }


def calculate_trade_levels(data: Dict) -> Dict:
    price = float(data["price"])
    atr_value = float(data["atr"])
    setup = data["setup"]

    if setup == "NO TRADE" or atr_value <= 0:
        return {
            "entry": None,
            "stop_loss": None,
            "tp1": None,
            "tp2": None,
            "rr": None,
        }

    risk_distance = atr_value

    if setup == "LONG":
        entry = price
        stop_loss = price - risk_distance
        tp1 = price + (risk_distance * 1.5)
        tp2 = price + (risk_distance * 2.5)
    else:
        entry = price
        stop_loss = price + risk_distance
        tp1 = price - (risk_distance * 1.5)
        tp2 = price - (risk_distance * 2.5)

    rr = abs(tp2 - entry) / abs(entry - stop_loss)

    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
    }


def analyze_candles(
    symbol: str,
    candles_4h: List,
    candles_1h: List,
    candles_15m: List,
) -> Dict:
    c4h = convert_candles(candles_4h)
    c1h = convert_candles(candles_1h)
    c15m = convert_candles(candles_15m)

    if len(c4h) < 10 or len(c1h) < 10 or len(c15m) < 60:
        raise ValueError("Not enough closed candle data for analysis")

    closes_15m = [candle["close"] for candle in c15m]
    current_price = closes_15m[-1]

    ema21 = ema(closes_15m, 21)
    ema50 = ema(closes_15m, 50)
    rsi_value = rsi(closes_15m)
    atr_value = atr(c15m)

    trend_4h = get_trend(c4h)
    structure_1h = get_structure(c1h)
    bos_15m = get_bos(c15m)
    support, resistance = get_support_resistance(c15m)
    volume = volume_status(c15m)

    if ema21 > ema50:
        ema_direction = "BULLISH"
    elif ema21 < ema50:
        ema_direction = "BEARISH"
    else:
        ema_direction = "NEUTRAL"

    data = {
        "symbol": symbol,
        "price": current_price,
        "trend_4h": trend_4h,
        "structure_1h": structure_1h,
        "bos_15m": bos_15m,
        "ema21": ema21,
        "ema50": ema50,
        "ema_direction": ema_direction,
        "rsi": rsi_value,
        "atr": atr_value,
        "volume": volume,
        "support": support,
        "resistance": resistance,
        "candle_time": c15m[-1].get("time"),
    }

    data.update(calculate_confluence(data))
    data.update(calculate_trade_levels(data))
    return data


async def analyze_symbol(market, symbol: str) -> Dict:
    ref = await market.resolve(symbol)

    candles_4h = await market.ohlcv(ref, "4H", 200)
    candles_1h = await market.ohlcv(ref, "1H", 200)
    candles_15m = await market.ohlcv(ref, "15M", 200)

    return analyze_candles(ref.symbol, candles_4h, candles_1h, candles_15m)
