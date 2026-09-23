from typing import List, Dict


def ema(values: List[float], period: int) -> float:
    if len(values) < period:
        raise ValueError("Not enough data for EMA")

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for price in values[period:]:
        result = ((price - result) * multiplier) + result

    return result


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        raise ValueError("Not enough data for RSI")

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1)) + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def average_volume(
    candles: List[Dict],
    period: int = 20,
) -> float:

    volumes = [
        float(candle["volume"])
        for candle in candles[-period:]
    ]

    return sum(volumes) / len(volumes)


def volume_status(
    candles: List[Dict],
    period: int = 20,
) -> str:

    if len(candles) < period + 1:
        return "UNKNOWN"

    current_volume = float(
        candles[-1]["volume"]
    )

    previous_average = average_volume(
        candles[:-1],
        period,
    )

    if current_volume > previous_average * 1.2:
        return "INCREASING"

    if current_volume < previous_average * 0.8:
        return "DECREASING"

    return "NORMAL"
    def atr(
    candles: List[Dict],
    period: int = 14,
) -> float:

    if len(candles) < period + 1:
        raise ValueError("Not enough data for ATR")

    true_ranges = []

    for i in range(1, len(candles)):

        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        previous_close = float(
            candles[i - 1]["close"]
        )

        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        true_ranges.append(true_range)

    return sum(
        true_ranges[-period:]
    ) / period
    
