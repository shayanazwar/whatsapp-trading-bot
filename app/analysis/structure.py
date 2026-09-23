from typing import List, Dict


def find_swings(
    candles: List[Dict],
    left: int = 2,
    right: int = 2,
):
    highs = []
    lows = []

    for i in range(left, len(candles) - right):

        high = float(candles[i]["high"])
        low = float(candles[i]["low"])

        left_highs = [
            float(candles[j]["high"])
            for j in range(i - left, i)
        ]

        right_highs = [
            float(candles[j]["high"])
            for j in range(i + 1, i + right + 1)
        ]

        left_lows = [
            float(candles[j]["low"])
            for j in range(i - left, i)
        ]

        right_lows = [
            float(candles[j]["low"])
            for j in range(i + 1, i + right + 1)
        ]

        if high > max(left_highs + right_highs):
            highs.append({
                "index": i,
                "price": high,
            })

        if low < min(left_lows + right_lows):
            lows.append({
                "index": i,
                "price": low,
            })

    return highs, lows


def get_structure(candles: List[Dict]) -> str:

    highs, lows = find_swings(candles)

    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"

    previous_high = highs[-2]["price"]
    latest_high = highs[-1]["price"]

    previous_low = lows[-2]["price"]
    latest_low = lows[-1]["price"]

    if latest_high > previous_high and latest_low > previous_low:
        return "HH/HL"

    if latest_high < previous_high and latest_low < previous_low:
        return "LH/LL"

    return "RANGE"


def get_trend(candles: List[Dict]) -> str:

    structure = get_structure(candles)

    if structure == "HH/HL":
        return "BULLISH"

    if structure == "LH/LL":
        return "BEARISH"

    return "SIDEWAYS"


def get_bos(candles: List[Dict]) -> str:

    highs, lows = find_swings(candles)

    if not highs or not lows:
        return "NONE"

    current_close = float(candles[-1]["close"])

    latest_high = highs[-1]["price"]
    latest_low = lows[-1]["price"]

    if current_close > latest_high:
        return "BULLISH BOS"

    if current_close < latest_low:
        return "BEARISH BOS"

    return "NONE"


def get_support_resistance(
    candles: List[Dict],
):
    highs, lows = find_swings(candles)

    current_price = float(
        candles[-1]["close"]
    )

    supports = [
        x["price"]
        for x in lows
        if x["price"] < current_price
    ]

    resistances = [
        x["price"]
        for x in highs
        if x["price"] > current_price
    ]

    support = max(supports) if supports else None
    resistance = min(resistances) if resistances else None

    return support, resistance
