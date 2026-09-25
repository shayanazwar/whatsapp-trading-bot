from __future__ import annotations

from typing import Any, Dict, List, Optional

from .indicators import atr, ema, rsi, volume_status
from .structure import (
    get_bos,
    get_structure,
    get_support_resistance,
    get_trend,
)


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


# ============================================================
# BASIC CANDLE CONVERSION
# ============================================================

def convert_candles(rows: List) -> List[Dict]:
    """Convert normalized MEXC Futures rows into analysis dictionaries."""

    candles: list[Dict] = []

    for row in rows:
        if len(row) < 6:
            continue

        try:
            item: Dict = {
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }

            if row[0] is not None:
                timestamp = int(row[0])

                if timestamp < 10**12:
                    timestamp *= 1000

                item["time"] = timestamp

            candles.append(item)

        except (TypeError, ValueError):
            continue

    return candles


# ============================================================
# CLOSED CANDLES ONLY
# ============================================================

def closed_candle_rows(
    rows: List,
    interval: str,
    now_ms: Optional[int] = None,
) -> List:

    if interval not in TIMEFRAME_MS:
        raise ValueError(
            f"Unsupported interval: {interval}"
        )

    import time

    now = int(
        now_ms
        if now_ms is not None
        else time.time() * 1000
    )

    interval_ms = TIMEFRAME_MS[interval]

    normalized: list[list] = []

    for row in rows:
        if len(row) < 6:
            continue

        try:
            timestamp = int(row[0])

            if timestamp < 10**12:
                timestamp *= 1000

            # Never use a candle that has not completely closed.
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

        except (TypeError, ValueError):
            continue

    normalized.sort(
        key=lambda item: item[0]
    )

    deduped: list[list] = []
    seen: set[int] = set()

    for row in normalized:
        if row[0] in seen:
            continue

        seen.add(row[0])
        deduped.append(row)

    return deduped


# ============================================================
# EMA HELPERS
# ============================================================

def _safe_ema(
    values: list[float],
    period: int,
) -> Optional[float]:

    if len(values) < period:
        return None

    try:
        return float(
            ema(values, period)
        )
    except Exception:
        return None


def _ema_slope(
    values: list[float],
    period: int,
    lookback: int = 5,
) -> float:

    if len(values) < period + lookback:
        return 0.0

    current = _safe_ema(
        values,
        period,
    )

    previous = _safe_ema(
        values[:-lookback],
        period,
    )

    if current is None or previous is None:
        return 0.0

    if previous == 0:
        return 0.0

    return (
        (current - previous)
        / abs(previous)
    )


# ============================================================
# RSI
# ============================================================

def _safe_rsi(
    values: list[float],
) -> float:

    try:
        value = float(
            rsi(values)
        )

        if value != value:
            return 50.0

        return value

    except Exception:
        return 50.0


# ============================================================
# ATR
# ============================================================

def _safe_atr(
    candles: list[Dict],
) -> float:

    try:
        value = float(
            atr(candles)
        )

        if value != value:
            return 0.0

        return value

    except Exception:
        return 0.0


# ============================================================
# ATR %
# ============================================================

def _atr_percent(
    price: float,
    atr_value: float,
) -> float:

    if price <= 0:
        return 0.0

    return (
        atr_value / price
    )


# ============================================================
# TRUE RANGE / ADX
# ============================================================

def _true_ranges(
    candles: list[Dict],
) -> list[float]:

    if not candles:
        return []

    result: list[float] = []

    previous_close: Optional[float] = None

    for candle in candles:

        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])

        if previous_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )

        result.append(
            max(0.0, tr)
        )

        previous_close = close

    return result


def _adx(
    candles: list[Dict],
    period: int = 14,
) -> float:

    if len(candles) < period * 2 + 2:
        return 0.0

    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        high = float(current["high"])
        low = float(current["low"])

        previous_high = float(
            previous["high"]
        )

        previous_low = float(
            previous["low"]
        )

        previous_close = float(
            previous["close"]
        )

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

        up_move = (
            high - previous_high
        )

        down_move = (
            previous_low - low
        )

        plus = (
            up_move
            if up_move > down_move
            and up_move > 0
            else 0.0
        )

        minus = (
            down_move
            if down_move > up_move
            and down_move > 0
            else 0.0
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(trs) < period:
        return 0.0

    def smooth(values: list[float]) -> list[float]:

        result: list[float] = []

        initial = sum(
            values[:period]
        )

        result.append(initial)

        previous = initial

        for value in values[period:]:
            previous = (
                previous
                - (previous / period)
                + value
            )

            result.append(previous)

        return result

    tr_s = smooth(trs)
    plus_s = smooth(plus_dm)
    minus_s = smooth(minus_dm)

    dx_values: list[float] = []

    for tr_value, plus_value, minus_value in zip(
        tr_s,
        plus_s,
        minus_s,
    ):

        if tr_value <= 0:
            continue

        plus_di = (
            100.0
            * plus_value
            / tr_value
        )

        minus_di = (
            100.0
            * minus_value
            / tr_value
        )

        denominator = (
            plus_di + minus_di
        )

        if denominator <= 0:
            dx = 0.0
        else:
            dx = (
                100.0
                * abs(
                    plus_di - minus_di
                )
                / denominator
            )

        dx_values.append(dx)

    if len(dx_values) < period:
        return 0.0

    return float(
        sum(
            dx_values[-period:]
        )
        / period
    )


# ============================================================
# RELATIVE VOLUME
# ============================================================

def _relative_volume(
    candles: list[Dict],
    period: int = 20,
) -> float:

    if len(candles) < period + 1:
        return 0.0

    current = float(
        candles[-1]["volume"]
    )

    previous = [
        float(candle["volume"])
        for candle in candles[
            -(period + 1):-1
        ]
    ]

    if not previous:
        return 0.0

    average = (
        sum(previous)
        / len(previous)
    )

    if average <= 0:
        return 0.0

    return (
        current / average
    )


# ============================================================
# SWINGS
# ============================================================

def _swing_highs(
    candles: list[Dict],
) -> list[tuple[int, float]]:

    swings: list[tuple[int, float]] = []

    for i in range(2, len(candles) - 2):

        high = float(
            candles[i]["high"]
        )

        if (
            high > float(candles[i - 1]["high"])
            and high > float(candles[i - 2]["high"])
            and high > float(candles[i + 1]["high"])
            and high > float(candles[i + 2]["high"])
        ):
            swings.append(
                (i, high)
            )

    return swings


def _swing_lows(
    candles: list[Dict],
) -> list[tuple[int, float]]:

    swings: list[tuple[int, float]] = []

    for i in range(2, len(candles) - 2):

        low = float(
            candles[i]["low"]
        )

        if (
            low < float(candles[i - 1]["low"])
            and low < float(candles[i - 2]["low"])
            and low < float(candles[i + 1]["low"])
            and low < float(candles[i + 2]["low"])
        ):
            swings.append(
                (i, low)
            )

    return swings


# ============================================================
# PROTECTED STRUCTURE
# ============================================================

def _protected_structure(
    candles: list[Dict],
) -> dict[str, Any]:

    highs = _swing_highs(candles)
    lows = _swing_lows(candles)

    result = {
        "bullish": False,
        "bearish": False,
        "protected_high": None,
        "protected_low": None,
        "state": "NEUTRAL",
    }

    if len(highs) >= 2:

        previous_high = highs[-2][1]
        latest_high = highs[-1][1]

        if latest_high > previous_high:
            result["bullish"] = True

    if len(lows) >= 2:

        previous_low = lows[-2][1]
        latest_low = lows[-1][1]

        if latest_low > previous_low:
            result["bullish"] = True

        if latest_low < previous_low:
            result["bearish"] = True

    if len(highs) >= 2:

        previous_high = highs[-2][1]
        latest_high = highs[-1][1]

        if latest_high < previous_high:
            result["bearish"] = True

    if lows:
        result["protected_low"] = lows[-1][1]

    if highs:
        result["protected_high"] = highs[-1][1]

    if result["bullish"] and not result["bearish"]:
        result["state"] = "BULLISH"

    elif result["bearish"] and not result["bullish"]:
        result["state"] = "BEARISH"

    return result


# ============================================================
# BOS
# ============================================================

def _confirmed_bos(
    candles: list[Dict],
    direction: str,
) -> bool:

    if len(candles) < 10:
        return False

    highs = _swing_highs(candles[:-2])
    lows = _swing_lows(candles[:-2])

    current_close = float(
        candles[-1]["close"]
    )

    atr_value = _safe_atr(
        candles
    )

    buffer = max(
        atr_value * 0.10,
        current_close * 0.0005,
    )

    if direction == "LONG" and highs:

        level = highs[-1][1]

        return (
            current_close
            > level + buffer
        )

    if direction == "SHORT" and lows:

        level = lows[-1][1]

        return (
            current_close
            < level - buffer
        )

    return False


# ============================================================
# PULLBACK / RETEST
# ============================================================

def _pullback_retest(
    candles: list[Dict],
    direction: str,
) -> bool:

    if len(candles) < 12:
        return False

    recent = candles[-6:]

    atr_value = _safe_atr(
        candles
    )

    if atr_value <= 0:
        return False

    highs = _swing_highs(
        candles[:-2]
    )

    lows = _swing_lows(
        candles[:-2]
    )

    if direction == "LONG" and highs:

        level = highs[-1][1]

        touched = any(
            abs(
                float(candle["low"])
                - level
            )
            <= atr_value * 0.35
            for candle in recent
        )

        reclaimed = (
            float(candles[-1]["close"])
            > level
        )

        return (
            touched
            and reclaimed
        )

    if direction == "SHORT" and lows:

        level = lows[-1][1]

        touched = any(
            abs(
                float(candle["high"])
                - level
            )
            <= atr_value * 0.35
            for candle in recent
        )

        reclaimed = (
            float(candles[-1]["close"])
            < level
        )

        return (
            touched
            and reclaimed
        )

    return False


# ============================================================
# CANDLE QUALITY
# ============================================================

def _trigger_quality(
    candle: Dict,
    direction: str,
) -> float:

    high = float(candle["high"])
    low = float(candle["low"])
    open_price = float(
        candle["open"]
    )
    close = float(
        candle["close"]
    )

    candle_range = high - low

    if candle_range <= 0:
        return 0.0

    body = abs(
        close - open_price
    )

    body_ratio = (
        body / candle_range
    )

    if direction == "LONG":
        close_position = (
            close - low
        ) / candle_range
    else:
        close_position = (
            high - close
        ) / candle_range

    return max(
        0.0,
        min(
            1.0,
            (
                body_ratio
                + close_position
            )
            / 2.0,
        ),
    )


# ============================================================
# 100-POINT SCORE
# ============================================================

def _build_score(
    *,
    direction_ok: bool,
    structure_ok: bool,
    setup_ok: bool,
    momentum_ok: bool,
    volume_ok: bool,
    location_ok: bool,
    futures_ok: bool,
    volatility_ok: bool,
) -> tuple[int, dict[str, int]]:

    groups = {
        "direction_regime": 20
        if direction_ok
        else 0,

        "market_structure": 20
        if structure_ok
        else 0,

        "setup_trigger": 20
        if setup_ok
        else 0,

        "momentum": 10
        if momentum_ok
        else 0,

        "volume_participation": 10
        if volume_ok
        else 0,

        "location_target_path": 10
        if location_ok
        else 0,

        "futures_context": 5
        if futures_ok
        else 0,

        "volatility_execution": 5
        if volatility_ok
        else 0,
    }

    return (
        sum(groups.values()),
        groups,
    )


# ============================================================
# TRADE LEVELS
# ============================================================

def calculate_trade_levels(
    data: Dict,
) -> Dict:

    price = float(
        data["price"]
    )

    atr_value = float(
        data["atr"]
    )

    setup = data["setup"]

    if (
        setup == "NO TRADE"
        or atr_value <= 0
    ):
        return {
            "entry": None,
            "stop_loss": None,
            "tp1": None,
            "tp2": None,
            "rr": None,
        }

    protected_low = data.get(
        "protected_low"
    )

    protected_high = data.get(
        "protected_high"
    )

    if setup == "LONG":

        candidates = [
            float(protected_low)
            if protected_low is not None
            else price - atr_value,

            price - atr_value,
        ]

        structure_sl = min(
            candidates
        )

        stop_loss = (
            structure_sl
            - (
                atr_value * 0.10
            )
        )

        risk = (
            price - stop_loss
        )

        if risk <= 0:
            risk = atr_value

            stop_loss = (
                price - risk
            )

        tp1 = price + (
            risk * 1.20
        )

        tp2 = price + (
            risk * 2.00
        )

    else:

        candidates = [
            float(protected_high)
            if protected_high is not None
            else price + atr_value,

            price + atr_value,
        ]

        structure_sl = max(
            candidates
        )

        stop_loss = (
            structure_sl
            + (
                atr_value * 0.10
            )
        )

        risk = (
            stop_loss - price
        )

        if risk <= 0:
            risk = atr_value

            stop_loss = (
                price + risk
            )

        tp1 = price - (
            risk * 1.20
        )

        tp2 = price - (
            risk * 2.00
        )

    if risk <= 0:
        rr = 0.0
    else:
        rr = abs(
            tp2 - price
        ) / risk

    return {
        "entry": price,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
    }


# ============================================================
# MAIN ANALYSIS ENGINE
# ============================================================

def analyze_candles(
    symbol: str,
    candles_4h: List,
    candles_1h: List,
    candles_15m: List,
) -> Dict:

    c4h = convert_candles(
        candles_4h
    )

    c1h = convert_candles(
        candles_1h
    )

    c15m = convert_candles(
        candles_15m
    )

    # We need enough history for EMA200 on 4H.
    if (
        len(c4h) < 205
        or len(c1h) < 205
        or len(c15m) < 80
    ):
        raise ValueError(
            "Not enough closed candle data for Gold Standard analysis"
        )

    closes_4h = [
        candle["close"]
        for candle in c4h
    ]

    closes_1h = [
        candle["close"]
        for candle in c1h
    ]

    closes_15m = [
        candle["close"]
        for candle in c15m
    ]

    current_price = float(
        closes_15m[-1]
    )

    # ========================================================
    # 4H REGIME
    # ========================================================

    ema21_4h = _safe_ema(
        closes_4h,
        21,
    )

    ema50_4h = _safe_ema(
        closes_4h,
        50,
    )

    ema100_4h = _safe_ema(
        closes_4h,
        100,
    )

    ema200_4h = _safe_ema(
        closes_4h,
        200,
    )

    atr_4h = _safe_atr(
        c4h
    )

    adx_4h = _adx(
        c4h
    )

    slope_50_4h = _ema_slope(
        closes_4h,
        50,
    )

    close_4h = closes_4h[-1]

    bullish_4h = (
        ema21_4h is not None
        and ema50_4h is not None
        and ema100_4h is not None
        and ema200_4h is not None
        and close_4h > ema200_4h
        and ema21_4h > ema50_4h
        and ema50_4h > ema100_4h
        and ema100_4h > ema200_4h
        and slope_50_4h > 0
        and adx_4h >= 25
    )

    bearish_4h = (
        ema21_4h is not None
        and ema50_4h is not None
        and ema100_4h is not None
        and ema200_4h is not None
        and close_4h < ema200_4h
        and ema21_4h < ema50_4h
        and ema50_4h < ema100_4h
        and ema100_4h < ema200_4h
        and slope_50_4h < 0
        and adx_4h >= 25
    )

    if bullish_4h:
        regime = "BULLISH"
    elif bearish_4h:
        regime = "BEARISH"
    else:
        regime = "NO_TRADE"

    # ========================================================
    # 1H DIRECTION + STRUCTURE
    # ========================================================

    ema21_1h = _safe_ema(
        closes_1h,
        21,
    )

    ema50_1h = _safe_ema(
        closes_1h,
        50,
    )

    structure_1h = get_structure(
        c1h
    )

    protected_1h = _protected_structure(
        c1h
    )

    close_1h = closes_1h[-1]

    long_1h = (
        regime == "BULLISH"
        and structure_1h == "HH/HL"
        and ema50_1h is not None
        and ema21_1h is not None
        and close_1h > ema50_1h
        and ema21_1h >= ema50_1h
        and protected_1h["state"]
        == "BULLISH"
    )

    short_1h = (
        regime == "BEARISH"
        and structure_1h == "LH/LL"
        and ema50_1h is not None
        and ema21_1h is not None
        and close_1h < ema50_1h
        and ema21_1h <= ema50_1h
        and protected_1h["state"]
        == "BEARISH"
    )

    # ========================================================
    # 15M SETUP
    # ========================================================

    bos_15m = get_bos(
        c15m
    )

    atr_15m = _safe_atr(
        c15m
    )

    rsi_15m = _safe_rsi(
        closes_15m
    )

    volume_15m = volume_status(
        c15m
    )

    rvol_15m = _relative_volume(
        c15m
    )

    long_bos = _confirmed_bos(
        c15m,
        "LONG",
    )

    short_bos = _confirmed_bos(
        c15m,
        "SHORT",
    )

    long_retest = _pullback_retest(
        c15m,
        "LONG",
    )

    short_retest = _pullback_retest(
        c15m,
        "SHORT",
    )

    long_setup = (
        long_1h
        and long_bos
        and long_retest
    )

    short_setup = (
        short_1h
        and short_bos
        and short_retest
    )

    if long_setup:
        setup = "LONG"

    elif short_setup:
        setup = "SHORT"

    else:
        setup = "NO TRADE"

    # ========================================================
    # MOMENTUM
    # ========================================================

    momentum_long = (
        setup == "LONG"
        and rsi_15m > 50
        and rsi_15m < 75
    )

    momentum_short = (
        setup == "SHORT"
        and rsi_15m < 50
        and rsi_15m > 25
    )

    momentum_ok = (
        momentum_long
        or momentum_short
    )

    # ========================================================
    # VOLUME / PARTICIPATION
    # ========================================================

    volume_ok = (
        volume_15m == "INCREASING"
        or rvol_15m >= 1.0
    )

    strong_volume = (
        rvol_15m >= 1.5
    )

    # ========================================================
    # LOCATION
    # ========================================================

    support, resistance = (
        get_support_resistance(
            c15m
        )
    )

    location_ok = True

    if setup == "LONG" and resistance is not None:
        # Don't enter directly into nearby resistance.
        location_ok = (
            current_price
            < float(resistance)
        )

    elif setup == "SHORT" and support is not None:
        location_ok = (
            current_price
            > float(support)
        )

    # ========================================================
    # VOLATILITY
    # ========================================================

    atr_pct_15m = _atr_percent(
        current_price,
        atr_15m,
    )

    volatility_ok = (
        atr_15m > 0
        and 0.002 <= atr_pct_15m <= 0.05
    )

    # ========================================================
    # FUTURES CONTEXT
    #
    # Scanner will later attach funding/orderbook/deals.
    # For now this group is neutral rather than fabricated.
    # ========================================================

    futures_context = "PENDING"

    futures_ok = True

    # ========================================================
    # STRUCTURE SCORE
    # ========================================================

    structure_ok = (
        (
            long_1h
            and protected_1h["state"]
            == "BULLISH"
        )
        or
        (
            short_1h
            and protected_1h["state"]
            == "BEARISH"
        )
    )

    direction_ok = (
        bullish_4h
        if setup == "LONG"
        else
        bearish_4h
        if setup == "SHORT"
        else False
    )

    setup_ok = (
        long_setup
        if setup == "LONG"
        else
        short_setup
        if setup == "SHORT"
        else False
    )

    # ========================================================
    # BUILD 100-POINT SCORE
    # ========================================================

    score, score_groups = _build_score(
        direction_ok=direction_ok,
        structure_ok=structure_ok,
        setup_ok=setup_ok,
        momentum_ok=momentum_ok,
        volume_ok=volume_ok,
        location_ok=location_ok,
        futures_ok=futures_ok,
        volatility_ok=volatility_ok,
    )

    # Strong trigger-quality bonus is informational for now.
    trigger_quality = _trigger_quality(
        c15m[-1],
        setup,
    ) if setup in {
        "LONG",
        "SHORT",
    } else 0.0

    # ========================================================
    # REASONS
    # ========================================================

    reasons: list[str] = []

    if bullish_4h:
        reasons.append(
            "4H bullish regime"
        )

    if bearish_4h:
        reasons.append(
            "4H bearish regime"
        )

    if long_1h:
        reasons.append(
            "1H bullish alignment"
        )

    if short_1h:
        reasons.append(
            "1H bearish alignment"
        )

    if long_bos:
        reasons.append(
            "15M bullish BOS"
        )

    if short_bos:
        reasons.append(
            "15M bearish BOS"
        )

    if long_retest:
        reasons.append(
            "15M bullish retest"
        )

    if short_retest:
        reasons.append(
            "15M bearish retest"
        )

    if momentum_ok:
        reasons.append(
            "15M momentum aligned"
        )

    if strong_volume:
        reasons.append(
            "RVOL >= 1.5"
        )

    if location_ok:
        reasons.append(
            "Location acceptable"
        )

    if volatility_ok:
        reasons.append(
            "Volatility acceptable"
        )

    # ========================================================
    # TRADE LEVELS
    # ========================================================

    data: Dict[str, Any] = {
        "symbol": symbol,
        "price": current_price,

        "setup": setup,

        "trend_4h": (
            "BULLISH"
            if bullish_4h
            else
            "BEARISH"
            if bearish_4h
            else
            "NO_TRADE"
        ),

        "regime": regime,

        "structure_1h": structure_1h,

        "protected_structure_1h": (
            protected_1h["state"]
        ),

        "protected_high": (
            protected_1h["protected_high"]
        ),

        "protected_low": (
            protected_1h["protected_low"]
        ),

        "bos_15m": bos_15m,

        "ema21": (
            _safe_ema(
                closes_15m,
                21,
            )
        ),

        "ema50": (
            _safe_ema(
                closes_15m,
                50,
            )
        ),

        "ema21_4h": ema21_4h,
        "ema50_4h": ema50_4h,
        "ema100_4h": ema100_4h,
        "ema200_4h": ema200_4h,

        "ema21_1h": ema21_1h,
        "ema50_1h": ema50_1h,

        "rsi": rsi_15m,

        "atr": atr_15m,

        "atr_4h": atr_4h,

        "atr_pct": atr_pct_15m,

        "adx_4h": adx_4h,

        "ema50_slope_4h": slope_50_4h,

        "volume": volume_15m,

        "rvol": rvol_15m,

        "support": support,

        "resistance": resistance,

        "futures_context": futures_context,

        "trigger_quality": trigger_quality,

        "score": score,

        "score_groups": score_groups,

        "bullish_points": (
            1
            if bullish_4h
            else 0
        ),

        "bearish_points": (
            1
            if bearish_4h
            else 0
        ),

        "direction_ok": direction_ok,
        "structure_ok": structure_ok,
        "setup_ok": setup_ok,
        "momentum_ok": momentum_ok,
        "volume_ok": volume_ok,
        "location_ok": location_ok,
        "futures_ok": futures_ok,
        "volatility_ok": volatility_ok,

        "reasons": reasons,

        "candle_time": (
            c15m[-1].get("time")
        ),
    }

    data.update(
        calculate_trade_levels(
            data
        )
    )

    return data


# ============================================================
# LEGACY MARKET ANALYSIS HELPER
# ============================================================

async def analyze_symbol(
    market,
    symbol: str,
) -> Dict:

    ref = await market.resolve(
        symbol
    )

    candles_4h = await market.ohlcv(
        ref,
        "4H",
        250,
    )

    candles_1h = await market.ohlcv(
        ref,
        "1H",
        250,
    )

    candles_15m = await market.ohlcv(
        ref,
        "15M",
        250,
    )

    return analyze_candles(
        ref.symbol,
        candles_4h,
        candles_1h,
        candles_15m,
    )
