from __future__ import annotations

from typing import Any, Dict, List, Optional
import time

from .indicators import atr, ema, rsi, volume_status
from .structure import (
    get_bos,
    get_structure,
    get_support_resistance,
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
    candles: list[Dict] = []

    for row in rows:
        if len(row) < 6:
            continue

        try:
            timestamp = int(row[0])

            if timestamp < 10**12:
                timestamp *= 1000

            candles.append(
                {
                    "time": timestamp,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )

        except (TypeError, ValueError):
            continue

    candles.sort(
        key=lambda item: item["time"]
    )

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
# EMA
# ============================================================

def _safe_ema(
    values: list[float],
    period: int,
) -> Optional[float]:

    if len(values) < period:
        return None

    try:
        value = float(
            ema(values, period)
        )

        if value != value:
            return None

        return value

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

    if (
        current is None
        or previous is None
        or previous == 0
    ):
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


def _atr_percent(
    price: float,
    atr_value: float,
) -> float:

    if price <= 0:
        return 0.0

    return atr_value / price


# ============================================================
# ADX
# ============================================================

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
            if (
                up_move > down_move
                and up_move > 0
            )
            else 0.0
        )

        minus = (
            down_move
            if (
                down_move > up_move
                and down_move > 0
            )
            else 0.0
        )

        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    def smooth(
        values: list[float],
    ) -> list[float]:

        if len(values) < period:
            return []

        result = []

        previous = sum(
            values[:period]
        )

        result.append(previous)

        for value in values[period:]:
            previous = (
                previous
                - previous / period
                + value
            )

            result.append(previous)

        return result

    tr_s = smooth(trs)
    plus_s = smooth(plus_dm)
    minus_s = smooth(minus_dm)

    if not tr_s:
        return 0.0

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
            dx_values.append(0.0)
            continue

        dx_values.append(
            100.0
            * abs(
                plus_di - minus_di
            )
            / denominator
        )

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

    average = (
        sum(previous)
        / len(previous)
        if previous
        else 0.0
    )

    if average <= 0:
        return 0.0

    return current / average


# ============================================================
# CONFIRMED SWINGS
# ============================================================

def _swing_highs(
    candles: list[Dict],
) -> list[tuple[int, float]]:

    result = []

    for i in range(
        2,
        len(candles) - 2,
    ):

        high = float(
            candles[i]["high"]
        )

        if (
            high > float(
                candles[i - 1]["high"]
            )
            and high > float(
                candles[i - 2]["high"]
            )
            and high > float(
                candles[i + 1]["high"]
            )
            and high > float(
                candles[i + 2]["high"]
            )
        ):
            result.append(
                (i, high)
            )

    return result


def _swing_lows(
    candles: list[Dict],
) -> list[tuple[int, float]]:

    result = []

    for i in range(
        2,
        len(candles) - 2,
    ):

        low = float(
            candles[i]["low"]
        )

        if (
            low < float(
                candles[i - 1]["low"]
            )
            and low < float(
                candles[i - 2]["low"]
            )
            and low < float(
                candles[i + 1]["low"]
            )
            and low < float(
                candles[i + 2]["low"]
            )
        ):
            result.append(
                (i, low)
            )

    return result


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
        "protected_high": (
            highs[-1][1]
            if highs
            else None
        ),
        "protected_low": (
            lows[-1][1]
            if lows
            else None
        ),
        "state": "NEUTRAL",
    }

    if (
        len(highs) >= 2
        and len(lows) >= 2
    ):

        higher_high = (
            highs[-1][1]
            > highs[-2][1]
        )

        higher_low = (
            lows[-1][1]
            > lows[-2][1]
        )

        lower_high = (
            highs[-1][1]
            < highs[-2][1]
        )

        lower_low = (
            lows[-1][1]
            < lows[-2][1]
        )

        if higher_high and higher_low:
            result["bullish"] = True

        if lower_high and lower_low:
            result["bearish"] = True

    if (
        result["bullish"]
        and not result["bearish"]
    ):
        result["state"] = "BULLISH"

    elif (
        result["bearish"]
        and not result["bullish"]
    ):
        result["state"] = "BEARISH"

    return result


# ============================================================
# CONFIRMED BOS
# ============================================================

def _confirmed_bos(
    candles: list[Dict],
    direction: str,
) -> tuple[bool, Optional[float]]:

    if len(candles) < 15:
        return False, None

    # Exclude the latest candle when identifying the
    # structural level. The latest candle performs the break.
    structure_candles = candles[:-1]

    highs = _swing_highs(
        structure_candles
    )

    lows = _swing_lows(
        structure_candles
    )

    close = float(
        candles[-1]["close"]
    )

    atr_value = _safe_atr(
        candles
    )

    buffer = max(
        atr_value * 0.10,
        close * 0.0005,
    )

    if direction == "LONG" and highs:

        level = highs[-1][1]

        return (
            close > level + buffer,
            level,
        )

    if direction == "SHORT" and lows:

        level = lows[-1][1]

        return (
            close < level - buffer,
            level,
        )

    return False, None


# ============================================================
# PULLBACK / RETEST
# ============================================================

def _pullback_retest(
    candles: list[Dict],
    direction: str,
    level: Optional[float],
) -> bool:

    if (
        len(candles) < 8
        or level is None
    ):
        return False

    atr_value = _safe_atr(
        candles
    )

    if atr_value <= 0:
        return False

    recent = candles[-6:]

    tolerance = (
        atr_value * 0.35
    )

    if direction == "LONG":

        touched = any(
            float(candle["low"])
            <= level + tolerance
            and float(candle["low"])
            >= level - tolerance
            for candle in recent
        )

        reclaimed = (
            float(candles[-1]["close"])
            > level
        )

        return touched and reclaimed

    if direction == "SHORT":

        touched = any(
            float(candle["high"])
            >= level - tolerance
            and float(candle["high"])
            <= level + tolerance
            for candle in recent
        )

        reclaimed = (
            float(candles[-1]["close"])
            < level
        )

        return touched and reclaimed

    return False


# ============================================================
# 5M TRIGGER
# ============================================================

def _five_minute_trigger(
    candles: list[Dict],
    direction: str,
    setup_level: Optional[float],
) -> dict[str, Any]:

    result = {
        "ready": False,
        "long": False,
        "short": False,
        "quality": 0.0,
        "rsi": 50.0,
        "rvol": 0.0,
        "body_ratio": 0.0,
        "break_level": None,
        "candle_time": None,
    }

    if len(candles) < 30:
        return result

    current = candles[-1]
    previous = candles[-2]

    high = float(
        current["high"]
    )

    low = float(
        current["low"]
    )

    open_price = float(
        current["open"]
    )

    close = float(
        current["close"]
    )

    previous_high = float(
        previous["high"]
    )

    previous_low = float(
        previous["low"]
    )

    candle_range = (
        high - low
    )

    if candle_range <= 0:
        return result

    body_ratio = (
        abs(close - open_price)
        / candle_range
    )

    close_position_long = (
        close - low
    ) / candle_range

    close_position_short = (
        high - close
    ) / candle_range

    rsi_5m = _safe_rsi(
        [
            float(c["close"])
            for c in candles
        ]
    )

    rvol_5m = _relative_volume(
        candles
    )

    atr_5m = _safe_atr(
        candles
    )

    if direction == "LONG":

        broke_previous_high = (
            close > previous_high
        )

        above_setup = (
            setup_level is None
            or close > setup_level
        )

        quality = (
            body_ratio * 0.40
            + close_position_long * 0.35
            + min(
                rvol_5m / 2.0,
                1.0,
            ) * 0.25
        )

        trigger = (
            broke_previous_high
            and above_setup
            and body_ratio >= 0.55
            and close_position_long >= 0.65
            and rsi_5m > 50
            and rsi_5m < 75
            and rvol_5m >= 1.0
        )

        result.update(
            {
                "ready": trigger,
                "long": trigger,
                "short": False,
                "quality": quality,
                "rsi": rsi_5m,
                "rvol": rvol_5m,
                "body_ratio": body_ratio,
                "break_level": previous_high,
                "candle_time": current["time"],
                "atr": atr_5m,
            }
        )

    elif direction == "SHORT":

        broke_previous_low = (
            close < previous_low
        )

        below_setup = (
            setup_level is None
            or close < setup_level
        )

        quality = (
            body_ratio * 0.40
            + close_position_short * 0.35
            + min(
                rvol_5m / 2.0,
                1.0,
            ) * 0.25
        )

        trigger = (
            broke_previous_low
            and below_setup
            and body_ratio >= 0.55
            and close_position_short >= 0.65
            and rsi_5m < 50
            and rsi_5m > 25
            and rvol_5m >= 1.0
        )

        result.update(
            {
                "ready": trigger,
                "long": False,
                "short": trigger,
                "quality": quality,
                "rsi": rsi_5m,
                "rvol": rvol_5m,
                "body_ratio": body_ratio,
                "break_level": previous_low,
                "candle_time": current["time"],
                "atr": atr_5m,
            }
        )

    return result


# ============================================================
# TARGET PATH
# ============================================================

def _target_path(
    *,
    side: str,
    entry: float,
    risk: float,
    resistance: Optional[float],
    support: Optional[float],
) -> tuple[bool, Optional[float], Optional[float]]:

    if risk <= 0:
        return False, None, None

    if side == "LONG":

        tp1 = entry + (
            risk * 1.20
        )

        tp2 = entry + (
            risk * 2.00
        )

        if (
            resistance is not None
            and float(resistance) <= tp2
        ):
            return (
                False,
                tp1,
                tp2,
            )

        return True, tp1, tp2

    if side == "SHORT":

        tp1 = entry - (
            risk * 1.20
        )

        tp2 = entry - (
            risk * 2.00
        )

        if (
            support is not None
            and float(support) >= tp2
        ):
            return (
                False,
                tp1,
                tp2,
            )

        return True, tp1, tp2

    return False, None, None


# ============================================================
# TRADE LEVELS
# ============================================================

def calculate_trade_levels(
    data: Dict,
) -> Dict:

    setup = data.get(
        "setup"
    )

    price = float(
        data.get("price") or 0
    )

    atr_value = float(
        data.get("atr") or 0
    )

    if (
        setup not in {
            "LONG",
            "SHORT",
        }
        or price <= 0
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

        if protected_low is not None:
            stop_loss = (
                float(protected_low)
                - atr_value * 0.10
            )
        else:
            stop_loss = (
                price
                - atr_value
            )

        risk = (
            price - stop_loss
        )

        if risk <= 0:
            return {
                "entry": None,
                "stop_loss": None,
                "tp1": None,
                "tp2": None,
                "rr": None,
            }

        path_ok, tp1, tp2 = (
            _target_path(
                side="LONG",
                entry=price,
                risk=risk,
                resistance=data.get(
                    "resistance"
                ),
                support=data.get(
                    "support"
                ),
            )
        )

    else:

        if protected_high is not None:
            stop_loss = (
                float(protected_high)
                + atr_value * 0.10
            )
        else:
            stop_loss = (
                price
                + atr_value
            )

        risk = (
            stop_loss - price
        )

        if risk <= 0:
            return {
                "entry": None,
                "stop_loss": None,
                "tp1": None,
                "tp2": None,
                "rr": None,
            }

        path_ok, tp1, tp2 = (
            _target_path(
                side="SHORT",
                entry=price,
                risk=risk,
                resistance=data.get(
                    "resistance"
                ),
                support=data.get(
                    "support"
                ),
            )
        )

    if (
        not path_ok
        or tp1 is None
        or tp2 is None
    ):
        return {
            "entry": price,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "rr": 0.0,
        }

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
        "direction_regime": (
            20 if direction_ok else 0
        ),
        "market_structure": (
            20 if structure_ok else 0
        ),
        "setup_trigger": (
            20 if setup_ok else 0
        ),
        "momentum": (
            10 if momentum_ok else 0
        ),
        "volume_participation": (
            10 if volume_ok else 0
        ),
        "location_target_path": (
            10 if location_ok else 0
        ),
        "futures_context": (
            5 if futures_ok else 0
        ),
        "volatility_execution": (
            5 if volatility_ok else 0
        ),
    }

    return (
        sum(groups.values()),
        groups,
    )


# ============================================================
# MAIN ANALYSIS ENGINE
# ============================================================

def analyze_candles(
    symbol: str,
    candles_4h: List,
    candles_1h: List,
    candles_15m: List,
    candles_5m: Optional[List] = None,
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

    c5m = convert_candles(
        candles_5m or []
    )

    # ========================================================
    # DATA REQUIREMENTS
    # ========================================================

    if len(c4h) < 205:
        raise ValueError(
            "Not enough 4H candles"
        )

    if len(c1h) < 205:
        raise ValueError(
            "Not enough 1H candles"
        )

    if len(c15m) < 80:
        raise ValueError(
            "Not enough 15M candles"
        )

    if len(c5m) < 30:
        raise ValueError(
            "Not enough 5M candles"
        )

    closes_4h = [
        c["close"]
        for c in c4h
    ]

    closes_1h = [
        c["close"]
        for c in c1h
    ]

    closes_15m = [
        c["close"]
        for c in c15m
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

    regime = (
        "BULLISH"
        if bullish_4h
        else
        "BEARISH"
        if bearish_4h
        else
        "NO_TRADE"
    )

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

    protected_1h = (
        _protected_structure(
            c1h
        )
    )

    close_1h = closes_1h[-1]

    long_1h = (
        regime == "BULLISH"
        and structure_1h == "HH/HL"
        and ema21_1h is not None
        and ema50_1h is not None
        and close_1h > ema50_1h
        and ema21_1h >= ema50_1h
        and protected_1h["state"]
        == "BULLISH"
    )

    short_1h = (
        regime == "BEARISH"
        and structure_1h == "LH/LL"
        and ema21_1h is not None
        and ema50_1h is not None
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

    long_bos, long_bos_level = (
        _confirmed_bos(
            c15m,
            "LONG",
        )
    )

    short_bos, short_bos_level = (
        _confirmed_bos(
            c15m,
            "SHORT",
        )
    )

    long_retest = _pullback_retest(
        c15m,
        "LONG",
        long_bos_level,
    )

    short_retest = _pullback_retest(
        c15m,
        "SHORT",
        short_bos_level,
    )

    long_setup_candidate = (
        long_1h
        and long_bos
        and long_retest
    )

    short_setup_candidate = (
        short_1h
        and short_bos
        and short_retest
    )

    # ========================================================
    # 5M TRIGGER
    # ========================================================

    if long_setup_candidate:
        five_trigger = (
            _five_minute_trigger(
                c5m,
                "LONG",
                long_bos_level,
            )
        )

    elif short_setup_candidate:
        five_trigger = (
            _five_minute_trigger(
                c5m,
                "SHORT",
                short_bos_level,
            )
        )

    else:
        five_trigger = (
            _five_minute_trigger(
                c5m,
                "NONE",
                None,
            )
        )

    # ========================================================
    # FINAL SETUP
    # ========================================================

    long_setup = (
        long_setup_candidate
        and five_trigger["long"]
    )

    short_setup = (
        short_setup_candidate
        and five_trigger["short"]
    )

    if long_setup:
        setup = "LONG"

    elif short_setup:
        setup = "SHORT"

    else:
        setup = "NO TRADE"

    # ========================================================
    # DIRECTION
    # ========================================================

    direction_ok = (
        (
            setup == "LONG"
            and bullish_4h
            and long_1h
        )
        or
        (
            setup == "SHORT"
            and bearish_4h
            and short_1h
        )
    )

    # ========================================================
    # STRUCTURE
    # ========================================================

    structure_ok = (
        (
            setup == "LONG"
            and structure_1h == "HH/HL"
            and protected_1h["state"]
            == "BULLISH"
            and long_bos
        )
        or
        (
            setup == "SHORT"
            and structure_1h == "LH/LL"
            and protected_1h["state"]
            == "BEARISH"
            and short_bos
        )
    )

    # ========================================================
    # SETUP
    # ========================================================

    setup_ok = (
        (
            setup == "LONG"
            and long_bos
            and long_retest
            and five_trigger["long"]
        )
        or
        (
            setup == "SHORT"
            and short_bos
            and short_retest
            and five_trigger["short"]
        )
    )

    # ========================================================
    # MOMENTUM
    # ========================================================

    momentum_ok = (
        (
            setup == "LONG"
            and rsi_15m > 50
            and rsi_15m < 75
            and float(
                five_trigger["rsi"]
            ) > 50
        )
        or
        (
            setup == "SHORT"
            and rsi_15m < 50
            and rsi_15m > 25
            and float(
                five_trigger["rsi"]
            ) < 50
        )
    )

    # ========================================================
    # VOLUME
    # ========================================================

    volume_ok = (
        rvol_15m >= 1.0
        and float(
            five_trigger["rvol"]
        ) >= 1.0
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

    if setup == "LONG":

        if resistance is not None:
            location_ok = (
                current_price
                < float(resistance)
            )

    elif setup == "SHORT":

        if support is not None:
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
        and 0.002
        <= atr_pct_15m
        <= 0.05
    )

    # ========================================================
    # FUTURES CONTEXT
    #
    # The scanner must replace this after retrieving real
    # MEXC funding/orderbook/trade-flow information.
    #
    # Until that happens it is deliberately FALSE rather
    # than pretending context exists.
    # ========================================================

    futures_context = "PENDING"
    futures_ok = False

    # ========================================================
    # BULLISH / BEARISH EVIDENCE
    #
    # Four independent directional confirmations:
    #
    # 1. 4H regime
    # 2. 1H structure
    # 3. 1H EMA alignment
    # 4. 15M setup direction
    # ========================================================

    bullish_points = 0
    bearish_points = 0

    if bullish_4h:
        bullish_points += 1

    if bearish_4h:
        bearish_points += 1

    if (
        structure_1h == "HH/HL"
        and protected_1h["state"]
        == "BULLISH"
    ):
        bullish_points += 1

    if (
        structure_1h == "LH/LL"
        and protected_1h["state"]
        == "BEARISH"
    ):
        bearish_points += 1

    if (
        ema21_1h is not None
        and ema50_1h is not None
        and ema21_1h >= ema50_1h
        and close_1h > ema50_1h
    ):
        bullish_points += 1

    if (
        ema21_1h is not None
        and ema50_1h is not None
        and ema21_1h <= ema50_1h
        and close_1h < ema50_1h
    ):
        bearish_points += 1

    if setup == "LONG":
        bullish_points += 1

    if setup == "SHORT":
        bearish_points += 1

    # ========================================================
    # PRELIMINARY TARGET PATH
    # ========================================================

    preliminary_data = {
        "setup": setup,
        "price": current_price,
        "atr": atr_15m,
        "protected_low": protected_1h[
            "protected_low"
        ],
        "protected_high": protected_1h[
            "protected_high"
        ],
        "support": support,
        "resistance": resistance,
    }

    preliminary_levels = (
        calculate_trade_levels(
            preliminary_data
        )
    )

    # ========================================================
    # LOCATION RECHECK USING TARGET PATH
    # ========================================================

    if setup in {
        "LONG",
        "SHORT",
    }:

        preliminary_tp2 = (
            preliminary_levels.get(
                "tp2"
            )
        )

        preliminary_sl = (
            preliminary_levels.get(
                "stop_loss"
            )
        )

        if (
            preliminary_tp2 is None
            or preliminary_sl is None
        ):
            location_ok = False

        else:

            risk = abs(
                current_price
                - float(preliminary_sl)
            )

            if risk <= 0:
                location_ok = False

            elif setup == "LONG":
                if (
                    resistance is not None
                    and float(resistance)
                    <= float(preliminary_tp2)
                ):
                    location_ok = False

            elif setup == "SHORT":
                if (
                    support is not None
                    and float(support)
                    >= float(preliminary_tp2)
                ):
                    location_ok = False

    # ========================================================
    # SCORE
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

    if five_trigger["ready"]:
        reasons.append(
            "5M trigger confirmed"
        )

    if momentum_ok:
        reasons.append(
            "Momentum aligned"
        )

    if volume_ok:
        reasons.append(
            "15M + 5M volume aligned"
        )

    if location_ok:
        reasons.append(
            "Target path acceptable"
        )

    if volatility_ok:
        reasons.append(
            "Volatility acceptable"
        )

    # ========================================================
    # FINAL DATA
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

        "long_bos_level": long_bos_level,
        "short_bos_level": short_bos_level,

        "long_retest": long_retest,
        "short_retest": short_retest,

        "ema21": _safe_ema(
            closes_15m,
            21,
        ),

        "ema50": _safe_ema(
            closes_15m,
            50,
        ),

        "ema21_4h": ema21_4h,
        "ema50_4h": ema50_4h,
        "ema100_4h": ema100_4h,
        "ema200_4h": ema200_4h,

        "ema21_1h": ema21_1h,
        "ema50_1h": ema50_1h,

        "rsi": rsi_15m,

        "rsi_5m": float(
            five_trigger["rsi"]
        ),

        "atr": atr_15m,

        "atr_4h": atr_4h,

        "atr_5m": float(
            five_trigger.get(
                "atr",
                0.0,
            )
        ),

        "atr_pct": atr_pct_15m,

        "adx_4h": adx_4h,

        "ema50_slope_4h": slope_50_4h,

        "volume": volume_15m,

        "rvol": rvol_15m,

        "rvol_15m": rvol_15m,

        "rvol_5m": float(
            five_trigger["rvol"]
        ),

        "support": support,
        "resistance": resistance,

        "futures_context": futures_context,

        "trigger_quality_5m": float(
            five_trigger["quality"]
        ),

        "trigger_quality": float(
            five_trigger["quality"]
        ),

        "five_minute_ready": bool(
            five_trigger["ready"]
        ),

        "five_minute_long": bool(
            five_trigger["long"]
        ),

        "five_minute_short": bool(
            five_trigger["short"]
        ),

        "closed_5m_candle_time": (
            five_trigger["candle_time"]
        ),

        "score": score,

        "score_groups": score_groups,

        "bullish_points": (
            bullish_points
        ),

        "bearish_points": (
            bearish_points
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
            c15m[-1]["time"]
        ),
    }

    # ========================================================
    # TRADE LEVELS
    # ========================================================

    data.update(
        calculate_trade_levels(
            data
        )
    )

    # ========================================================
    # FINAL TARGET-PATH VALIDATION
    # ========================================================

    if setup in {
        "LONG",
        "SHORT",
    }:

        if (
            data.get("entry") is None
            or data.get("stop_loss") is None
            or data.get("tp1") is None
            or data.get("tp2") is None
        ):
            data["location_ok"] = False
            data["score"] = max(
                0,
                data["score"]
                - score_groups[
                    "location_target_path"
                ],
            )

    # ========================================================
    # IMPORTANT:
    # Futures context is deliberately pending here.
    # scanner.py must attach real MEXC context and re-score
    # before validation.
    # ========================================================

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

    candles_5m = await market.ohlcv(
        ref,
        "5M",
        250,
    )

    return analyze_candles(
        ref.symbol,
        candles_4h,
        candles_1h,
        candles_15m,
        candles_5m,
    )
