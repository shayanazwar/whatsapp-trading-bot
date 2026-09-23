from typing import Dict, List

from .indicators import (
    ema,
    rsi,
    volume_status,
    atr,
)

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


def calculate_confluence(data: Dict) -> Dict:

    score = 0
    reasons = []

    # 4H trend
    if data["trend_4h"] == "BULLISH":
        score += 1
        reasons.append("4H bullish")

    elif data["trend_4h"] == "BEARISH":
        score += 1
        reasons.append("4H bearish")

    # 1H structure
    if data["structure_1h"] == "HH/HL":
        score += 1
        reasons.append("1H HH/HL")

    elif data["structure_1h"] == "LH/LL":
        score += 1
        reasons.append("1H LH/LL")

    # 15M BOS
    if data["bos_15m"] == "BULLISH BOS":
        score += 1
        reasons.append("15M bullish BOS")

    elif data["bos_15m"] == "BEARISH BOS":
        score += 1
        reasons.append("15M bearish BOS")

    # EMA
    if data["ema_direction"] == "BULLISH":
        score += 1
        reasons.append("EMA bullish")

    elif data["ema_direction"] == "BEARISH":
        score += 1
        reasons.append("EMA bearish")

    # RSI
    if 50 <= data["rsi"] <= 70:
        score += 1
        reasons.append("RSI bullish zone")

    elif 30 <= data["rsi"] < 50:
        score += 1
        reasons.append("RSI bearish zone")

    # Volume
    if data["volume"] == "INCREASING":
        score += 1
        reasons.append("Volume increasing")

    # Direction
    bullish_points = 0
    bearish_points = 0

    if data["trend_4h"] == "BULLISH":
        bullish_points += 1
    elif data["trend_4h"] == "BEARISH":
        bearish_points += 1

    if data["structure_1h"] == "HH/HL":
        bullish_points += 1
    elif data["structure_1h"] == "LH/LL":
        bearish_points += 1

    if data["bos_15m"] == "BULLISH BOS":
        bullish_points += 1
    elif data["bos_15m"] == "BEARISH BOS":
        bearish_points += 1

    if data["ema_direction"] == "BULLISH":
        bullish_points += 1
    elif data["ema_direction"] == "BEARISH":
        bearish_points += 1

    if data["rsi"] >= 50:
        bullish_points += 1
    else:
        bearish_points += 1

    if bullish_points > bearish_points:
        setup = "LONG"

    elif bearish_points > bullish_points:
        setup = "SHORT"

    else:
        setup = "NO TRADE"

    return {
        "score": score,
        "setup": setup,
        "reasons": reasons,
    }


def calculate_trade_levels(
    data: Dict,
) -> Dict:

    price = data["price"]
    atr_value = data["atr"]
    setup = data["setup"]

    # No setup = no trade levels
    if setup == "NO TRADE":
        return {
            "entry": None,
            "stop_loss": None,
            "tp1": None,
            "tp2": None,
            "rr": None,
        }

    # ATR-based risk distance
    risk_distance = atr_value * 1.0

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


async def analyze_symbol(
    market,
    symbol: str,
) -> Dict:

    ref = await market.resolve(symbol)

    # 4H
    candles_4h = convert_candles(
        await market.ohlcv(
            ref,
            "4H",
            200,
        )
    )

    # 1H
    candles_1h = convert_candles(
        await market.ohlcv(
            ref,
            "1H",
            200,
        )
    )

    # 15M
    candles_15m = convert_candles(
        await market.ohlcv(
            ref,
            "15M",
            200,
        )
    )

    closes_15m = [
        candle["close"]
        for candle in candles_15m
    ]

    # Indicators
    ema21 = ema(
        closes_15m,
        21,
    )

    ema50 = ema(
        closes_15m,
        50,
    )

    rsi_value = rsi(
        closes_15m
    )

    atr_value = atr(
        candles_15m
    )

    current_price = closes_15m[-1]

    # Market structure
    trend_4h = get_trend(
        candles_4h
    )

    structure_1h = get_structure(
        candles_1h
    )

    bos_15m = get_bos(
        candles_15m
    )

    support, resistance = get_support_resistance(
        candles_15m
    )

    volume = volume_status(
        candles_15m
    )

    # EMA direction
    if ema21 > ema50:
        ema_direction = "BULLISH"

    elif ema21 < ema50:
        ema_direction = "BEARISH"

    else:
        ema_direction = "NEUTRAL"

    data = {
        "symbol": ref.symbol,
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
    }

    # Confluence
    confluence = calculate_confluence(
        data
    )

    data.update(
        confluence
    )

    # Trade levels
    trade_levels = calculate_trade_levels(
        data
    )

    data.update(
        trade_levels
    )

    return data
