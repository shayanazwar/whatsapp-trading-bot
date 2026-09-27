from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_right
from dataclasses import dataclass

from ..analysis.engine import (
    ADX_TREND_MIN,
    EMA_TOLERANCE_PCT,
    MAX_ATR_PERCENTILE,
    MAX_SETUP_AGE_15M,
    MIN_ATR_PERCENTILE,
    MIN_TRIGGER_BODY,
    MIN_TRIGGER_RVOL,
    _bos_events,
    _five_minute_trigger,
    _pullback_retest,
    analyze_candles,
    build_btc_context,
    btc_filter_ok,
    convert_candles,
)
from ..automation.mexc_client import MexcClient
from ..automation.universe import MexcUniverse
from ..config import Settings
from .report import BacktestSummary, summarize
from .simulator import SimulatedTrade, simulate_trade


LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Historical warmups
# ---------------------------------------------------------------------------

MIN_4H_WARMUP_MS = 40 * 24 * 60 * 60 * 1000
MIN_1H_WARMUP_MS = 14 * 24 * 60 * 60 * 1000
MIN_15M_WARMUP_MS = 3 * 24 * 60 * 60 * 1000
MIN_5M_WARMUP_MS = 24 * 60 * 60 * 1000
MIN_1D_WARMUP_MS = 40 * 24 * 60 * 60 * 1000

# ---------------------------------------------------------------------------
# Timeframe sizes
# ---------------------------------------------------------------------------

M5_MS = 300_000
M15_MS = 900_000
M1H_MS = 3_600_000
M4H_MS = 14_400_000
M1D_MS = 86_400_000

MAX_KLINE_POINTS = 2000

INTERVALS = {
    "4h": "Hour4",
    "1h": "Min60",
    "15m": "Min15",
    "5m": "Min5",
    "1d": "Day1",
}

# ---------------------------------------------------------------------------
# Backtest execution safety / Free Render tuning
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 30
HISTORICAL_REQUEST_RETRIES = 3
HISTORICAL_RETRY_BACKOFF_SECONDS = 1.5

# Render Free is only 0.1 CPU / 512 MB. One CPU-bound historical worker is
# deliberately used even if main.py passes a larger value.
MAX_SYMBOL_CONCURRENCY = 1

SYMBOL_FETCH_TIMEOUT_SECONDS = 120
UNIVERSE_REFRESH_TIMEOUT_SECONDS = 60
ANALYSIS_TIMEOUT_SECONDS = 90
HEARTBEAT_INTERVAL_SECONDS = 30

# Keep a reference to the real engine trigger. Tests can monkeypatch the
# imported _five_minute_trigger; production uses the optimized path while
# patched/custom trigger tests use the compatibility path.
_ENGINE_FIVE_MINUTE_TRIGGER = _five_minute_trigger


class BacktestAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class SymbolHistory:
    symbol: str
    candles_4h: list[list[float | int]]
    candles_1h: list[list[float | int]]
    candles_15m: list[list[float | int]]
    candles_5m: list[list[float | int]]
    candles_1d: list[list[float | int]]
    # Exact production-path candidate timestamps prepared before the final
    # engine call. Kept optional for backward compatibility with tests and
    # callers that construct SymbolHistory manually.
    prefilter_candidates: tuple[tuple[int, "_SetupCandidate"], ...] = ()


@dataclass(frozen=True)
class _SetupCandidate:
    side: str
    bos_index: int
    bos_time: int
    bos_level: float
    bos_strength: float
    retest_index: int
    retest_time: int
    retest_low: float | None
    retest_high: float | None
    retest_quality: float


@dataclass(slots=True)
class _FrameMetrics:
    candles: list
    times: list[int]
    closes: list[float]
    volumes: list[float]
    ema21: list[float | None]
    ema50: list[float | None]
    ema100: list[float | None]
    ema200: list[float | None]
    ema50_slope: list[float]
    rsi: list[float]
    atr: list[float]
    adx: list[float]
    rvol: list[float]
    macd_hist: list[float]
    swing_highs: list[tuple[int, float]]
    swing_lows: list[tuple[int, float]]


def _ema_series_indexed(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for i in range(period, len(values)):
        current = alpha * values[i] + (1.0 - alpha) * current
        result[i] = current
    return result


def _rsi_series_indexed(values: list[float], period: int = 14) -> list[float]:
    result = [50.0] * len(values)
    if len(values) < period + 1:
        return result

    gains = [0.0] * (len(values) - 1)
    losses = [0.0] * (len(values) - 1)
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains[i - 1] = max(change, 0.0)
        losses[i - 1] = max(-change, 0.0)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value() -> float:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = value()
    for i in range(period + 1, len(values)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i - 1]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i - 1]) / period
        result[i] = value()
    return result


def _atr_series_indexed(candles: list, period: int = 14) -> list[float]:
    result = [0.0] * len(candles)
    if len(candles) <= period:
        return result

    true_ranges = [0.0] * len(candles)
    for i in range(1, len(candles)):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        previous_close = float(candles[i - 1]["close"])
        true_ranges[i] = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )

    rolling = sum(true_ranges[1 : period + 1])
    result[period] = rolling / period
    for i in range(period + 1, len(candles)):
        rolling += true_ranges[i] - true_ranges[i - period]
        result[i] = rolling / period
    return result


def _adx_series_indexed(candles: list, period: int = 14) -> list[float]:
    result = [0.0] * len(candles)
    if len(candles) < period * 2 + 2:
        return result

    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []

    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        high = float(cur["high"])
        low = float(cur["low"])
        previous_high = float(prev["high"])
        previous_low = float(prev["low"])
        previous_close = float(prev["close"])

        up = high - previous_high
        down = previous_low - low
        trs.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    tr_s = sum(trs[:period]) / period
    plus_s = sum(plus_dm[:period]) / period
    minus_s = sum(minus_dm[:period]) / period
    dx_values: list[float] = []
    adx_value = 0.0

    for i in range(period, len(trs)):
        tr_s = (tr_s * (period - 1) + trs[i]) / period
        plus_s = (plus_s * (period - 1) + plus_dm[i]) / period
        minus_s = (minus_s * (period - 1) + minus_dm[i]) / period

        pdi = 100.0 * plus_s / tr_s if tr_s else 0.0
        mdi = 100.0 * minus_s / tr_s if tr_s else 0.0
        den = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / den if den else 0.0
        dx_values.append(dx)

        # engine._adx() returns 0 until the source candle count reaches
        # period*2+2, then takes the mean of the first period DX values and
        # Wilder-smooths every later DX value.
        if len(dx_values) == period:
            adx_value = sum(dx_values) / period
        elif len(dx_values) > period:
            adx_value = (adx_value * (period - 1) + dx) / period

        candle_count = i + 2
        if candle_count >= period * 2 + 2:
            result[i + 1] = adx_value

    return result


def _relative_volume_series(candles: list, lookback: int = 20) -> list[float]:
    result = [0.0] * len(candles)
    if len(candles) < lookback + 1:
        return result

    volumes = [float(c["volume"]) for c in candles]
    rolling = sum(volumes[:lookback])
    for i in range(lookback, len(candles)):
        average = rolling / lookback
        result[i] = volumes[i] / average if average > 0 else 0.0
        rolling += volumes[i]
        rolling -= volumes[i - lookback]
    return result


def _macd_hist_series(candles: list) -> list[float]:
    closes = [float(c["close"]) for c in candles]
    n = len(closes)
    result = [0.0] * n
    ema12 = _ema_series_indexed(closes, 12)
    ema26 = _ema_series_indexed(closes, 26)

    line_values: list[float] = []
    signal: float | None = None
    signal_period = 9
    alpha = 2.0 / (signal_period + 1.0)

    for i in range(n):
        if ema12[i] is None or ema26[i] is None:
            continue
        line = float(ema12[i]) - float(ema26[i])
        line_values.append(line)

        if len(line_values) < signal_period:
            # The engine's _macd() uses signal=0 until its EMA(9) exists.
            result[i] = line
        elif len(line_values) == signal_period:
            signal = sum(line_values[:signal_period]) / signal_period
            result[i] = line - signal
        else:
            assert signal is not None
            signal = alpha * line + (1.0 - alpha) * signal
            result[i] = line - signal

    return result


def _swing_points_indexed(candles: list, left: int = 2, right: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(left, len(candles) - right):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        if (
            all(high > float(candles[j]["high"]) for j in range(i - left, i))
            and all(high > float(candles[j]["high"]) for j in range(i + 1, i + right + 1))
        ):
            highs.append((i, high))
        if (
            all(low < float(candles[j]["low"]) for j in range(i - left, i))
            and all(low < float(candles[j]["low"]) for j in range(i + 1, i + right + 1))
        ):
            lows.append((i, low))
    return highs, lows


def _build_frame_metrics(candles: list) -> _FrameMetrics:
    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]
    ema21 = _ema_series_indexed(closes, 21)
    ema50 = _ema_series_indexed(closes, 50)
    ema100 = _ema_series_indexed(closes, 100)
    ema200 = _ema_series_indexed(closes, 200)

    ema50_slope = [0.0] * len(candles)
    for i in range(5, len(candles)):
        current = ema50[i]
        base = ema50[i - 5]
        if current is not None and base is not None and abs(base) > 0:
            ema50_slope[i] = (current - base) / abs(base)

    swing_highs, swing_lows = _swing_points_indexed(candles)
    return _FrameMetrics(
        candles=candles,
        times=[_row_time(c) for c in candles],
        closes=closes,
        volumes=volumes,
        ema21=ema21,
        ema50=ema50,
        ema100=ema100,
        ema200=ema200,
        ema50_slope=ema50_slope,
        rsi=_rsi_series_indexed(closes),
        atr=_atr_series_indexed(candles),
        adx=_adx_series_indexed(candles),
        rvol=_relative_volume_series(candles),
        macd_hist=_macd_hist_series(candles),
        swing_highs=swing_highs,
        swing_lows=swing_lows,
    )


def _visible_pivots(
    pivots: list[tuple[int, float]],
    count: int,
) -> list[tuple[int, float]]:
    if count < 3:
        return []
    max_index = count - 3
    pos = bisect_right([idx for idx, _ in pivots], max_index)
    return pivots[:pos]


def _recent_swing_scores(
    frame: _FrameMetrics,
    count: int,
    lookback: int,
) -> dict[str, object]:
    start = max(0, count - lookback)
    minimum_index = start + 2
    max_index = count - 3

    highs = [
        item
        for item in frame.swing_highs
        if minimum_index <= item[0] <= max_index
    ]
    lows = [
        item
        for item in frame.swing_lows
        if minimum_index <= item[0] <= max_index
    ]

    result = {
        "bull_higher_high": False,
        "bull_higher_low": False,
        "bear_lower_high": False,
        "bear_lower_low": False,
        "bull_score": 0,
        "bear_score": 0,
    }
    if len(highs) >= 2:
        result["bull_higher_high"] = highs[-1][1] > highs[-2][1]
        result["bear_lower_high"] = highs[-1][1] < highs[-2][1]
    if len(lows) >= 2:
        result["bull_higher_low"] = lows[-1][1] > lows[-2][1]
        result["bear_lower_low"] = lows[-1][1] < lows[-2][1]
    result["bull_score"] = int(result["bull_higher_high"]) + int(result["bull_higher_low"])
    result["bear_score"] = int(result["bear_lower_high"]) + int(result["bear_lower_low"])
    return result


def _protected_state(
    frame: _FrameMetrics,
    count: int,
) -> dict[str, object]:
    highs = _visible_pivots(frame.swing_highs, count)
    lows = _visible_pivots(frame.swing_lows, count)
    ph = highs[-1][1] if highs else None
    pl = lows[-1][1] if lows else None
    if len(highs) < 2 or len(lows) < 2:
        return {"state": "NEUTRAL", "protected_high": ph, "protected_low": pl}

    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]
    state = (
        "BULLISH"
        if h2 > h1 and l2 > l1
        else "BEARISH"
        if h2 < h1 and l2 < l1
        else "NEUTRAL"
    )
    return {"state": state, "protected_high": ph, "protected_low": pl}


def _fast_four_hour_regime(frame: _FrameMetrics, count: int) -> dict[str, object]:
    if count <= 0:
        return {
            "bull": False, "bear": False, "regime": "NO_TRADE",
            "e21": None, "e50": None, "e100": None, "e200": None,
            "atr": 0.0, "adx": 0.0, "slope": 0.0,
            "protected": _protected_state(frame, count),
            "swings": _recent_swing_scores(frame, count, 80),
            "bull_votes": 0, "bear_votes": 0,
        }

    i = count - 1
    e21, e50, e100, e200 = frame.ema21[i], frame.ema50[i], frame.ema100[i], frame.ema200[i]
    atr_value = frame.atr[i]
    adx_value = frame.adx[i]
    slope = frame.ema50_slope[i]
    protected = _protected_state(frame, count)
    swings = _recent_swing_scores(frame, count, 80)

    base = {
        "bull": False, "bear": False, "regime": "NO_TRADE",
        "e21": e21, "e50": e50, "e100": e100, "e200": e200,
        "atr": atr_value, "adx": adx_value, "slope": slope,
        "protected": protected, "swings": swings,
        "bull_votes": 0, "bear_votes": 0,
    }
    if None in (e21, e50, e100, e200):
        return base

    current = frame.closes[i]
    bull_votes = sum((
        current > e200,
        e21 >= e50,
        e50 >= e100,
        slope > 0.0,
        protected["state"] == "BULLISH",
        swings["bull_score"] >= 1,
    ))
    bear_votes = sum((
        current < e200,
        e21 <= e50,
        e50 <= e100,
        slope < 0.0,
        protected["state"] == "BEARISH",
        swings["bear_score"] >= 1,
    ))
    bull = bool(
        current > e200
        and e21 >= e50
        and adx_value >= ADX_TREND_MIN
        and bull_votes >= 4
        and bull_votes > bear_votes
    )
    bear = bool(
        current < e200
        and e21 <= e50
        and adx_value >= ADX_TREND_MIN
        and bear_votes >= 4
        and bear_votes > bull_votes
    )
    base.update({
        "bull": bull,
        "bear": bear,
        "regime": "BULLISH" if bull else "BEARISH" if bear else "NO_TRADE",
        "bull_votes": bull_votes,
        "bear_votes": bear_votes,
    })
    return base


def _fast_one_hour_alignment(
    frame: _FrameMetrics,
    count: int,
    regime4: dict[str, object],
) -> dict[str, object]:
    if count <= 0:
        return {"long": False, "short": False, "long_votes": 0, "short_votes": 0}

    i = count - 1
    price = frame.closes[i]
    e21, e50, e200 = frame.ema21[i], frame.ema50[i], frame.ema200[i]
    protected = _protected_state(frame, count)
    swings = _recent_swing_scores(frame, count, 70)

    if e21 is None or e50 is None:
        return {"long": False, "short": False, "long_votes": 0, "short_votes": 0}

    # Exact get_structure() equivalent from the precomputed pivots.
    visible_highs = _visible_pivots(frame.swing_highs, count)
    visible_lows = _visible_pivots(frame.swing_lows, count)
    structure = "UNKNOWN"
    if len(visible_highs) >= 2 and len(visible_lows) >= 2:
        if (visible_highs[-1][1] > visible_highs[-2][1]
                and visible_lows[-1][1] > visible_lows[-2][1]):
            structure = "HH/HL"
        elif (visible_highs[-1][1] < visible_highs[-2][1]
              and visible_lows[-1][1] < visible_lows[-2][1]):
            structure = "LH/LL"
        else:
            structure = "RANGE"

    tolerance = price * EMA_TOLERANCE_PCT
    long_ema = price >= e50 - tolerance and e21 >= e50
    short_ema = price <= e50 + tolerance and e21 <= e50
    long_structure = structure == "HH/HL" or protected["state"] == "BULLISH" or swings["bull_score"] >= 1
    short_structure = structure == "LH/LL" or protected["state"] == "BEARISH" or swings["bear_score"] >= 1
    r = frame.rsi[i]
    long_momentum = r >= 50.0 and (e200 is None or price >= e200 * 0.995)
    short_momentum = r <= 50.0 and (e200 is None or price <= e200 * 1.005)
    slope = frame.ema50_slope[i]
    long_slope = slope >= -0.0010
    short_slope = slope <= 0.0010

    lv = int(long_ema) + int(long_structure) + int(long_momentum) + int(long_slope)
    sv = int(short_ema) + int(short_structure) + int(short_momentum) + int(short_slope)

    return {
        "long": bool(regime4.get("bull") and lv >= 3 and lv > sv),
        "short": bool(regime4.get("bear") and sv >= 3 and sv > lv),
        "long_votes": lv,
        "short_votes": sv,
        "structure": structure,
        "protected": protected,
        "swings": swings,
    }


def _atr_percentile_at(frame: _FrameMetrics, index: int, lookback: int = 100, period: int = 14) -> float:
    # Exact _atr_percentile() fallback for short histories.
    if index < 0 or index >= len(frame.candles):
        return 50.0
    if index + 1 < period + 10:
        return 50.0

    start = max(period, index - lookback + 1)
    ratios: list[float] = []
    for i in range(start, index + 1):
        price = frame.closes[i]
        atr_value = frame.atr[i]
        if price > 0 and atr_value > 0:
            ratios.append(atr_value / price)
    if not ratios:
        return 50.0
    current = ratios[-1]
    return 100.0 * sum(value <= current for value in ratios) / len(ratios)


def _cheap_15m_directional_filters(
    frame: _FrameMetrics,
    index: int,
    side: str,
) -> bool:
    r = frame.rsi[index]
    rv = frame.rvol[index]
    macd_hist = frame.macd_hist[index]
    price = frame.closes[index]
    atr_value = frame.atr[index]

    if side == "LONG":
        if not (50.0 < r < 78.0 and macd_hist >= 0.0):
            return False
    elif side == "SHORT":
        if not (22.0 < r < 50.0 and macd_hist <= 0.0):
            return False
    else:
        return False

    if rv < 0.90:
        return False
    atr_pct = atr_value / price if price > 0 else 0.0
    if not (atr_value > 0 and 0.0005 <= atr_pct <= 0.05):
        return False

    atr_rank = _atr_percentile_at(frame, index)
    return MIN_ATR_PERCENTILE <= atr_rank <= MAX_ATR_PERCENTILE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _align_5m_timestamp(timestamp_ms: int) -> int:
    return (int(timestamp_ms) // M5_MS) * M5_MS


def _valid_candle(row: object) -> bool:
    if not isinstance(row, (list, tuple)) or len(row) < 6:
        return False
    try:
        timestamp = int(float(row[0]))
        float(row[1])
        float(row[2])
        float(row[3])
        float(row[4])
        float(row[5])
    except (TypeError, ValueError, IndexError):
        return False
    return timestamp >= 0


def _row_time(row: object) -> int:
    if isinstance(row, dict):
        return int(row["time"])
    return int(float(row[0]))


def _closed_slice(rows: list, timeframe_ms: int, now_ms: int) -> list:
    if not rows:
        return []
    cutoff = int(now_ms) - int(timeframe_ms)
    end_index = bisect_right(rows, cutoff, key=_row_time)
    return rows[:end_index]


def _convert_for_engine(
    rows: list[list[float | int]],
    symbol: str,
    timeframe: str,
) -> list:
    converted = convert_candles(rows)
    if rows and not converted:
        raise ValueError(
            f"{symbol}: {timeframe} candle conversion produced 0 valid rows "
            f"from {len(rows)} raw rows"
        )
    return converted


def _ceil_to_m5(timestamp_ms: int) -> int:
    timestamp_ms = int(timestamp_ms)
    return ((timestamp_ms + M5_MS - 1) // M5_MS) * M5_MS


# ---------------------------------------------------------------------------
# Historical pagination
# ---------------------------------------------------------------------------


async def _fetch_range(
    client: MexcClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[float | int]]:
    interval_sizes = {
        "Min5": M5_MS,
        "Min15": M15_MS,
        "Min60": M1H_MS,
        "Hour4": M4H_MS,
        "Day1": M1D_MS,
    }

    if interval not in interval_sizes:
        raise ValueError(f"Unsupported backtest interval: {interval}")

    interval_ms = interval_sizes[interval]
    start_ms = int(start_ms)
    end_ms = int(end_ms)

    if end_ms < start_ms:
        return []

    cursor = (start_ms // interval_ms) * interval_ms
    final = (end_ms // interval_ms) * interval_ms
    page_span = interval_ms * (MAX_KLINE_POINTS - 1)

    result: dict[int, list[float | int]] = {}
    request_count = 0

    while cursor <= final:
        page_end = min(final, cursor + page_span)
        request_count += 1

        rows = None
        last_error: Exception | None = None

        for attempt in range(1, HISTORICAL_REQUEST_RETRIES + 1):
            try:
                rows = await asyncio.wait_for(
                    client.get_klines_range(
                        symbol,
                        interval,
                        cursor,
                        page_end,
                        limit=MAX_KLINE_POINTS,
                    ),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                last_error = None
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                LOGGER.warning(
                    "BACKTEST RANGE RETRY | %s %s attempt=%d/%d error=%s: %s",
                    symbol,
                    interval,
                    attempt,
                    HISTORICAL_REQUEST_RETRIES,
                    type(exc).__name__,
                    exc,
                )
                if attempt < HISTORICAL_REQUEST_RETRIES:
                    await asyncio.sleep(
                        HISTORICAL_RETRY_BACKOFF_SECONDS * attempt
                    )

        if last_error is not None:
            raise RuntimeError(
                f"{symbol} {interval}: MEXC range request failed after "
                f"{HISTORICAL_REQUEST_RETRIES} attempts: "
                f"{type(last_error).__name__}: {last_error}"
            ) from last_error

        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(
                f"{symbol} {interval}: invalid MEXC kline response type "
                f"{type(rows).__name__}"
            )

        valid_timestamps: list[int] = []
        for row in rows:
            if not _valid_candle(row):
                continue
            timestamp = int(float(row[0]))
            if cursor <= timestamp <= page_end:
                result[timestamp] = list(row)
                valid_timestamps.append(timestamp)

        if not rows or not valid_timestamps:
            cursor = page_end + interval_ms
            continue

        last_timestamp = max(valid_timestamps)
        next_cursor = last_timestamp + interval_ms
        if next_cursor <= cursor:
            next_cursor = page_end + interval_ms
        cursor = next_cursor

    final_rows = [result[t] for t in sorted(result)]

    LOGGER.debug(
        "BACKTEST DATA | %s | %s | requests=%d candles=%d",
        symbol,
        interval,
        request_count,
        len(final_rows),
    )
    return final_rows


async def _fetch_timeframe(
    client: MexcClient,
    symbol: str,
    timeframe: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list[float | int]]:
    started = time.monotonic()

    LOGGER.info(
        "BACKTEST TF FETCH START | symbol=%s timeframe=%s start=%d end=%d",
        symbol,
        timeframe,
        start_ms,
        end_ms,
    )

    rows = await _fetch_range(
        client,
        symbol,
        interval,
        start_ms,
        end_ms,
    )

    LOGGER.info(
        "BACKTEST TF FETCH DONE | symbol=%s timeframe=%s candles=%d seconds=%.2f",
        symbol,
        timeframe,
        len(rows),
        time.monotonic() - started,
    )
    return rows


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BacktestRunner:
    """
    Historical technical paper backtest.

    No live orders are placed.

    Historical OHLCV can reproduce the deterministic technical engine and
    historical BTC filter, but cannot recreate live-only orderbook, spread,
    ticker-freshness, funding/deals and execution-quality checks.

    Performance design:
      MEXC -> 4H/1H/15M prefilter -> 5M trigger -> full engine -> simulator.

    5M and 1D history are deliberately fetched only when earlier stages leave
    a plausible setup window. On the Render Free plan this saves both CPU and
    network work across the 300-symbol universe.
    """

    def __init__(
        self,
        client: MexcClient,
        universe: MexcUniverse,
        settings: Settings,
        *,
        max_concurrency: int = MAX_SYMBOL_CONCURRENCY,
    ) -> None:
        self.client = client
        self.universe = universe
        self.settings = settings
        self.max_concurrency = max(
            1,
            min(int(max_concurrency), MAX_SYMBOL_CONCURRENCY),
        )
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    # -----------------------------------------------------------------------
    # Heartbeat
    # -----------------------------------------------------------------------

    async def _heartbeat(
        self,
        *,
        days: int,
        state: dict[str, object],
        started: float,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                now = time.monotonic()
                last_activity = float(state.get("last_activity", started))
                LOGGER.info(
                    "BACKTEST HEARTBEAT | days=%d phase=%s "
                    "processed=%d/%d tested=%d errors=%d signals=%d "
                    "worker=%s symbol=%s idle=%.1fs elapsed=%.1fs",
                    days,
                    str(state.get("phase", "UNKNOWN")),
                    int(state.get("processed", 0)),
                    int(state.get("total", 0)),
                    int(state.get("tested", 0)),
                    int(state.get("errors", 0)),
                    int(state.get("signals", 0)),
                    str(state.get("worker", "-")),
                    str(state.get("current_symbol", "-")),
                    max(0.0, now - last_activity),
                    now - started,
                )

    # -----------------------------------------------------------------------
    # Main backtest
    # -----------------------------------------------------------------------

    async def run(self, days: int) -> BacktestSummary:
        days = int(days)

        if days not in {7, 30, 90}:
            raise ValueError("Supported backtests: 7D, 30D, 90D")

        if self._lock.locked():
            raise BacktestAlreadyRunning(
                "A backtest is already running. Please wait for it to finish."
            )

        async with self._lock:
            started = time.monotonic()
            heartbeat_stop = asyncio.Event()
            heartbeat_task: asyncio.Task | None = None
            workers: list[asyncio.Task] = []

            state: dict[str, object] = {
                "phase": "INITIALIZING",
                "total": 0,
                "processed": 0,
                "tested": 0,
                "errors": 0,
                "signals": 0,
                "worker": "-",
                "current_symbol": "-",
                "last_activity": started,
            }

            def touch(
                *,
                phase: str | None = None,
                symbol: str | None = None,
                worker: int | str | None = None,
            ) -> None:
                if phase is not None:
                    state["phase"] = phase
                if symbol is not None:
                    state["current_symbol"] = symbol
                if worker is not None:
                    state["worker"] = worker
                state["last_activity"] = time.monotonic()

            heartbeat_task = asyncio.create_task(
                self._heartbeat(
                    days=days,
                    state=state,
                    started=started,
                    stop_event=heartbeat_stop,
                ),
                name="backtest-heartbeat",
            )

            trades: list[SimulatedTrade] = []

            try:
                now_ms = int(time.time() * 1000)
                period_end = _align_5m_timestamp(now_ms)
                period_start = period_end - days * 24 * 60 * 60 * 1000

                LOGGER.info(
                    "BACKTEST INIT | days=%d period_start=%d period_end=%d",
                    days,
                    period_start,
                    period_end,
                )

                # -----------------------------------------------------------
                # Universe
                # -----------------------------------------------------------

                touch(phase="UNIVERSE", symbol="-", worker="-")
                LOGGER.info("BACKTEST UNIVERSE FETCH START | days=%d", days)

                try:
                    symbols = await asyncio.wait_for(
                        self.universe.refresh(),
                        timeout=UNIVERSE_REFRESH_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    raise TimeoutError(
                        "MEXC backtest universe refresh timed out after "
                        f"{UNIVERSE_REFRESH_TIMEOUT_SECONDS}s"
                    ) from exc

                symbols = list(symbols or [])[:300]
                if not symbols:
                    raise RuntimeError(
                        "No eligible MEXC Futures symbols are available for backtesting."
                    )

                state["total"] = len(symbols)

                LOGGER.info(
                    "BACKTEST START | days=%d symbols=%d start=%d end=%d",
                    days,
                    len(symbols),
                    period_start,
                    period_end,
                )
                LOGGER.info(
                    "BACKTEST UNIVERSE READY | symbols=%d concurrency=%d "
                    "request_timeout=%ss symbol_timeout=%ss analysis_timeout=%ss",
                    len(symbols),
                    self.max_concurrency,
                    REQUEST_TIMEOUT_SECONDS,
                    SYMBOL_FETCH_TIMEOUT_SECONDS,
                    ANALYSIS_TIMEOUT_SECONDS,
                )

                # -----------------------------------------------------------
                # BTC history: fetched once and reused for BTC symbol + BTC
                # filter. This removes the duplicate BTC download that the
                # old runner performed.
                # -----------------------------------------------------------

                touch(phase="BTC DATA", symbol="BTC_USDT", worker="-")
                LOGGER.info("BACKTEST BTC FETCH START | days=%d", days)

                btc_history = await asyncio.wait_for(
                    self._fetch_btc_history(period_start, period_end),
                    timeout=SYMBOL_FETCH_TIMEOUT_SECONDS,
                )

                LOGGER.info(
                    "BACKTEST BTC FETCH DONE | 4H=%d 1H=%d 15M=%d 5M=%d 1D=%d",
                    len(btc_history.candles_4h),
                    len(btc_history.candles_1h),
                    len(btc_history.candles_15m),
                    len(btc_history.candles_5m),
                    len(btc_history.candles_1d),
                )

                btc_engine_cache = (
                    _convert_for_engine(
                        btc_history.candles_4h, "BTC_USDT", "4H"
                    ),
                    _convert_for_engine(
                        btc_history.candles_1h, "BTC_USDT", "1H"
                    ),
                    _convert_for_engine(
                        btc_history.candles_15m, "BTC_USDT", "15M"
                    ),
                )

                if (
                    len(btc_engine_cache[0]) < 205
                    or len(btc_engine_cache[1]) < 205
                    or len(btc_engine_cache[2]) < 80
                ):
                    raise ValueError(
                        "BTC historical context is insufficient for the deterministic engine"
                    )

                btc_engine_times = (
                    [_row_time(row) for row in btc_engine_cache[0]],
                    [_row_time(row) for row in btc_engine_cache[1]],
                    [_row_time(row) for row in btc_engine_cache[2]],
                )
                btc_context_cache: dict[tuple[int, int, int], tuple[dict, str]] = {}

                # -----------------------------------------------------------
                # Rolling worker queue
                # -----------------------------------------------------------

                touch(phase="SYMBOL DATA", symbol="-", worker="-")
                queue: asyncio.Queue[str | None] = asyncio.Queue()

                for symbol in symbols:
                    queue.put_nowait(symbol)
                for _ in range(self.max_concurrency):
                    queue.put_nowait(None)

                state_lock = asyncio.Lock()

                async def worker(worker_id: int) -> None:
                    while True:
                        symbol = await queue.get()
                        try:
                            if symbol is None:
                                return

                            touch(
                                phase="SYMBOL DATA",
                                symbol=symbol,
                                worker=worker_id,
                            )

                            symbol_started = time.monotonic()
                            try:
                                # BTC was already fetched for global BTC context.
                                # Reuse it instead of making the same five API
                                # requests again.
                                if str(symbol).upper() == "BTC_USDT":
                                    history = btc_history
                                    LOGGER.info(
                                        "BACKTEST DATA REUSE | worker=%d symbol=%s | BTC history reused",
                                        worker_id,
                                        symbol,
                                    )
                                else:
                                    touch(
                                        phase="PREFILTER",
                                        symbol=symbol,
                                        worker=worker_id,
                                    )
                                    history = await asyncio.wait_for(
                                        self._prepare_symbol_history(
                                            symbol,
                                            period_start,
                                            period_end,
                                            touch,
                                        ),
                                        timeout=SYMBOL_FETCH_TIMEOUT_SECONDS + ANALYSIS_TIMEOUT_SECONDS,
                                    )

                                touch(
                                    phase="ANALYSIS",
                                    symbol=symbol,
                                    worker=worker_id,
                                )
                                LOGGER.info(
                                    "BACKTEST ANALYSIS START | worker=%d symbol=%s",
                                    worker_id,
                                    symbol,
                                )

                                analysis_started = time.monotonic()

                                symbol_trades = await asyncio.wait_for(
                                    asyncio.to_thread(
                                        self._backtest_symbol,
                                        history,
                                        period_start,
                                        period_end,
                                        btc_history,
                                        btc_engine_cache,
                                        btc_engine_times,
                                        btc_context_cache,
                                    ),
                                    timeout=ANALYSIS_TIMEOUT_SECONDS,
                                )

                                LOGGER.info(
                                    "BACKTEST ANALYSIS DONE | worker=%d symbol=%s "
                                    "| signals=%d seconds=%.2f total_symbol=%.2f",
                                    worker_id,
                                    symbol,
                                    len(symbol_trades),
                                    time.monotonic() - analysis_started,
                                    time.monotonic() - symbol_started,
                                )

                                trades.extend(symbol_trades)

                                async with state_lock:
                                    state["tested"] = int(state["tested"]) + 1
                                    state["signals"] = len(trades)

                            except asyncio.CancelledError:
                                LOGGER.warning(
                                    "BACKTEST SYMBOL CANCELLED | worker=%d symbol=%s",
                                    worker_id,
                                    symbol,
                                )
                                raise

                            except Exception as exc:
                                async with state_lock:
                                    state["errors"] = int(state["errors"]) + 1

                                LOGGER.exception(
                                    "BACKTEST symbol failed: %s | %s",
                                    symbol,
                                    exc,
                                )
                                LOGGER.error(
                                    "BACKTEST DATA ERROR | %s | %s: %s",
                                    symbol,
                                    type(exc).__name__,
                                    exc,
                                )

                            finally:
                                async with state_lock:
                                    state["processed"] = int(state["processed"]) + 1
                                    processed = int(state["processed"])
                                    tested = int(state["tested"])
                                    errors = int(state["errors"])
                                    signal_count = int(state["signals"])

                                touch(
                                    phase="SYMBOL DATA",
                                    symbol=symbol,
                                    worker=worker_id,
                                )

                                LOGGER.info(
                                    "BACKTEST SYMBOL COMPLETE | processed=%d/%d "
                                    "| tested=%d | errors=%d | signals=%d | symbol=%s",
                                    processed,
                                    len(symbols),
                                    tested,
                                    errors,
                                    signal_count,
                                    symbol,
                                )
                                LOGGER.info(
                                    "BACKTEST PROGRESS | days=%d processed=%d/%d "
                                    "tested=%d errors=%d signals=%d",
                                    days,
                                    processed,
                                    len(symbols),
                                    tested,
                                    errors,
                                    signal_count,
                                )

                                await asyncio.sleep(0)

                        finally:
                            queue.task_done()

                workers = [
                    asyncio.create_task(
                        worker(worker_id),
                        name=f"backtest-worker-{worker_id}",
                    )
                    for worker_id in range(self.max_concurrency)
                ]

                try:
                    await asyncio.gather(*workers)
                except BaseException:
                    for task in workers:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    raise

                # -----------------------------------------------------------
                # Final report
                # -----------------------------------------------------------

                touch(phase="FINALIZING", symbol="-", worker="-")
                trades.sort(key=lambda trade: trade.signal_time_ms)

                summary = summarize(
                    days=days,
                    coins_selected=len(symbols),
                    coins_tested=int(state["tested"]),
                    data_errors=int(state["errors"]),
                    trades=trades,
                )

                LOGGER.info(
                    "BACKTEST COMPLETE | days=%d tested=%d errors=%d signals=%d duration=%.2fs",
                    days,
                    int(state["tested"]),
                    int(state["errors"]),
                    len(trades),
                    time.monotonic() - started,
                )
                return summary

            except asyncio.CancelledError:
                LOGGER.warning(
                    "BACKTEST CANCELLED | days=%d duration=%.2fs",
                    days,
                    time.monotonic() - started,
                )
                raise

            finally:
                for task in workers:
                    if not task.done():
                        task.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)

                heartbeat_stop.set()
                if heartbeat_task is not None:
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

    # -----------------------------------------------------------------------
    # Staged historical data pipeline
    # -----------------------------------------------------------------------

    async def _prepare_symbol_history(
        self,
        symbol: str,
        period_start: int,
        period_end: int,
        touch,
    ) -> SymbolHistory:
        """Fetch progressively: 4H/1H/15M -> 5M -> 1D.

        5M and 1D are skipped completely when the earlier pipeline stages
        prove that no directional setup window can produce a signal.
        """
        starts = {
            "4h": period_start - MIN_4H_WARMUP_MS,
            "1h": period_start - MIN_1H_WARMUP_MS,
            "15m": period_start - MIN_15M_WARMUP_MS,
            "5m": period_start - MIN_5M_WARMUP_MS,
            "1d": period_start - MIN_1D_WARMUP_MS,
        }

        # ---------------------------------------------------------------
        # Stage 1: 4H + 1H + 15M only
        # ---------------------------------------------------------------

        LOGGER.info(
            "BACKTEST PREFILTER FETCH START | symbol=%s | 4H/1H/15M",
            symbol,
        )

        c4_raw_task = asyncio.create_task(
            _fetch_timeframe(
                self.client,
                symbol,
                "4H",
                INTERVALS["4h"],
                starts["4h"],
                period_end,
            )
        )
        c1_raw_task = asyncio.create_task(
            _fetch_timeframe(
                self.client,
                symbol,
                "1H",
                INTERVALS["1h"],
                starts["1h"],
                period_end,
            )
        )
        c15_raw_task = asyncio.create_task(
            _fetch_timeframe(
                self.client,
                symbol,
                "15M",
                INTERVALS["15m"],
                starts["15m"],
                period_end,
            )
        )

        try:
            c4_raw, c1_raw, c15_raw = await asyncio.gather(
                c4_raw_task,
                c1_raw_task,
                c15_raw_task,
            )
        except asyncio.CancelledError:
            for task in (c4_raw_task, c1_raw_task, c15_raw_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                c4_raw_task,
                c1_raw_task,
                c15_raw_task,
                return_exceptions=True,
            )
            raise
        except Exception as exc:
            for task in (c4_raw_task, c1_raw_task, c15_raw_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                c4_raw_task,
                c1_raw_task,
                c15_raw_task,
                return_exceptions=True,
            )
            raise RuntimeError(
                f"{symbol}: prefilter historical data fetch failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        c4 = _convert_for_engine(c4_raw, symbol, "4H")
        c1 = _convert_for_engine(c1_raw, symbol, "1H")
        c15 = _convert_for_engine(c15_raw, symbol, "15M")

        if len(c4) < 205:
            raise ValueError(
                f"{symbol}: insufficient 4H candles ({len(c4)} < 205)"
            )
        if len(c1) < 205:
            raise ValueError(
                f"{symbol}: insufficient 1H candles ({len(c1)} < 205)"
            )
        if len(c15) < 80:
            raise ValueError(
                f"{symbol}: insufficient 15M candles ({len(c15)} < 80)"
            )

        # Ask only whether any exact 5M time window can survive the 4H/1H/
        # 15M directional gates. No 5M or 1D API calls happen before this.
        setup_candidates = await asyncio.to_thread(
            self._build_setup_candidates,
            c15,
        )
        probe = await asyncio.to_thread(
            self._find_directional_signal_windows,
            c4,
            c1,
            c15,
            period_start,
            period_end,
            setup_candidates,
        )

        LOGGER.info(
            "BACKTEST PREFILTER DONE | symbol=%s | directional_windows=%d",
            symbol,
            len(probe),
        )

        if not probe:
            LOGGER.info(
                "BACKTEST 5M FETCH SKIP | symbol=%s | reason=no_directional_setup_window",
                symbol,
            )
            return SymbolHistory(
                symbol=symbol,
                candles_4h=c4_raw,
                candles_1h=c1_raw,
                candles_15m=c15_raw,
                candles_5m=[],
                candles_1d=[],
            )

        # ---------------------------------------------------------------
        # Stage 2: 5M only after higher-timeframe setup survives
        # ---------------------------------------------------------------

        c5_raw = await _fetch_timeframe(
            self.client,
            symbol,
            "5M",
            INTERVALS["5m"],
            starts["5m"],
            period_end,
        )
        c5 = _convert_for_engine(c5_raw, symbol, "5M")
        if len(c5) < 30:
            raise ValueError(
                f"{symbol}: insufficient 5M candles ({len(c5)} < 30)"
            )

        candidate_times = await asyncio.to_thread(
            self._find_5m_candidate_times,
            c4,
            c1,
            c15,
            c5,
            probe,
            period_start,
            period_end,
            setup_candidates,
        )

        LOGGER.info(
            "BACKTEST 5M PREFILTER | symbol=%s | candidates=%d",
            symbol,
            len(candidate_times),
        )

        if not candidate_times:
            LOGGER.info(
                "BACKTEST 1D FETCH SKIP | symbol=%s | reason=no_5m_trigger",
                symbol,
            )
            return SymbolHistory(
                symbol=symbol,
                candles_4h=c4_raw,
                candles_1h=c1_raw,
                candles_15m=c15_raw,
                candles_5m=c5_raw,
                candles_1d=[],
            )

        # ---------------------------------------------------------------
        # Stage 3: 1D only when a real 5M trigger exists
        # ---------------------------------------------------------------

        c1d_raw = await _fetch_timeframe(
            self.client,
            symbol,
            "1D",
            INTERVALS["1d"],
            starts["1d"],
            period_end,
        )

        return SymbolHistory(
            symbol=symbol,
            candles_4h=c4_raw,
            candles_1h=c1_raw,
            candles_15m=c15_raw,
            candles_5m=c5_raw,
            candles_1d=c1d_raw,
            prefilter_candidates=tuple(candidate_times),
        )

    # -----------------------------------------------------------------------
    # Setup / direction prefilter
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_setup_candidates(c15: list) -> tuple[list[_SetupCandidate], list[_SetupCandidate]]:
        result: dict[str, list[_SetupCandidate]] = {"LONG": [], "SHORT": []}

        for side in ("LONG", "SHORT"):
            try:
                bos_events = _bos_events(
                    c15,
                    side,
                    lookback=len(c15),
                )
            except Exception:
                LOGGER.exception(
                    "BACKTEST BOS failed | side=%s",
                    side,
                )
                continue

            for bos in bos_events:
                try:
                    retest = _pullback_retest(
                        c15,
                        side,
                        bos,
                        MAX_SETUP_AGE_15M,
                    )
                except Exception:
                    LOGGER.exception(
                        "BACKTEST RETEST failed | side=%s",
                        side,
                    )
                    continue

                if not retest.get("valid"):
                    continue

                try:
                    bos_index = int(bos["index"])
                    bos_time = int(bos["time"])
                    bos_level = float(bos["level"])
                    bos_strength = float(bos.get("strength") or 0.0)
                    retest_index = int(retest["index"])
                    retest_time = int(retest["time"])
                    retest_quality = float(retest.get("quality") or 0.0)
                    retest_low = (
                        float(retest["low"]) if retest.get("low") is not None else None
                    )
                    retest_high = (
                        float(retest["high"]) if retest.get("high") is not None else None
                    )
                except (KeyError, TypeError, ValueError):
                    continue

                result[side].append(
                    _SetupCandidate(
                        side=side,
                        bos_index=bos_index,
                        bos_time=bos_time,
                        bos_level=bos_level,
                        bos_strength=bos_strength,
                        retest_index=retest_index,
                        retest_time=retest_time,
                        retest_low=retest_low,
                        retest_high=retest_high,
                        retest_quality=retest_quality,
                    )
                )

        result["LONG"].sort(key=lambda item: item.bos_index)
        result["SHORT"].sort(key=lambda item: item.bos_index)
        return result["LONG"], result["SHORT"]

    @staticmethod
    def _latest_setup(
        setups: list[_SetupCandidate],
        bos_indices: list[int],
        latest_c15_index: int,
    ) -> _SetupCandidate | None:
        if not setups:
            return None

        pos = bisect_right(bos_indices, latest_c15_index) - 1
        while pos >= 0:
            setup = setups[pos]

            # Same age rule used by _select_latest_bos_with_retest().
            if latest_c15_index - setup.bos_index > MAX_SETUP_AGE_15M + 2:
                break

            if (
                setup.retest_index <= latest_c15_index
                and latest_c15_index - setup.retest_index <= MAX_SETUP_AGE_15M
            ):
                return setup

            pos -= 1

        return None

    @staticmethod
    def _direction_state_at(
        c4: list,
        c1: list,
        c4_times: list[int],
        c1_times: list[int],
        signal_close_time: int,
        cache: dict[tuple[int, int], tuple[dict, dict]],
    ) -> tuple[dict, dict] | None:
        c4_end = bisect_right(
            c4_times,
            int(signal_close_time) - M4H_MS,
        )
        c1_end = bisect_right(
            c1_times,
            int(signal_close_time) - M1H_MS,
        )

        if c4_end < 205 or c1_end < 205:
            return None

        key = (c4_end, c1_end)
        cached = cache.get(key)
        if cached is not None:
            return cached

        regime = _four_hour_regime(c4[:c4_end])
        alignment = _one_hour_alignment(c1[:c1_end], regime)
        cache[key] = (regime, alignment)
        return regime, alignment

    @classmethod
    def _select_directional_setup(
        cls,
        frame4: _FrameMetrics,
        frame1: _FrameMetrics,
        c15_times: list[int],
        signal_close_time: int,
        long_setups: list[_SetupCandidate],
        short_setups: list[_SetupCandidate],
        direction_cache: dict[tuple[int, int], tuple[dict, dict]],
    ) -> _SetupCandidate | None:
        latest_c15_index = bisect_right(
            c15_times,
            int(signal_close_time) - M15_MS,
        ) - 1
        if latest_c15_index < 0:
            return None

        c4_end = bisect_right(frame4.times, int(signal_close_time) - M4H_MS)
        c1_end = bisect_right(frame1.times, int(signal_close_time) - M1H_MS)
        if c4_end < 205 or c1_end < 205:
            return None

        key = (c4_end, c1_end)
        direction_state = direction_cache.get(key)
        if direction_state is None:
            regime = _fast_four_hour_regime(frame4, c4_end)
            alignment = _fast_one_hour_alignment(frame1, c1_end, regime)
            direction_state = (regime, alignment)
            direction_cache[key] = direction_state
        if direction_state is None:
            return None

        regime, alignment = direction_state
        if not (regime.get("bull") or regime.get("bear")):
            return None

        long_setup = cls._latest_setup(
            long_setups,
            [item.bos_index for item in long_setups],
            latest_c15_index,
        )
        short_setup = cls._latest_setup(
            short_setups,
            [item.bos_index for item in short_setups],
            latest_c15_index,
        )

        long_candidate = bool(alignment.get("long") and long_setup)
        short_candidate = bool(alignment.get("short") and short_setup)

        if long_candidate and not short_candidate:
            return long_setup
        if short_candidate and not long_candidate:
            return short_setup
        if long_candidate and short_candidate:
            return (
                long_setup
                if long_setup is not None
                and short_setup is not None
                and long_setup.bos_strength >= short_setup.bos_strength
                else short_setup
            )
        return None

    @classmethod
    def _find_directional_signal_windows(
        cls,
        c4: list,
        c1: list,
        c15: list,
        period_start: int,
        period_end: int,
        setup_candidates: tuple[list[_SetupCandidate], list[_SetupCandidate]] | None = None,
    ) -> list[tuple[int, _SetupCandidate]]:
        """Find exact potential signal closes before downloading 5M candles."""
        frame4 = _build_frame_metrics(c4)
        frame1 = _build_frame_metrics(c1)
        c15_times = [_row_time(row) for row in c15]

        if setup_candidates is None:
            long_setups, short_setups = cls._build_setup_candidates(c15)
        else:
            long_setups, short_setups = setup_candidates
        if not long_setups and not short_setups:
            return []

        direction_cache: dict[tuple[int, int], tuple[dict, dict]] = {}
        candidate_map: dict[int, _SetupCandidate] = {}

        all_setups = long_setups + short_setups
        all_setups.sort(key=lambda item: (item.retest_time, item.bos_index, item.side))

        for setup in all_setups:
            if setup.retest_time > period_end:
                continue
            if setup.retest_time + M15_MS > period_end:
                continue

            trigger_start = max(
                period_start,
                setup.retest_time + M15_MS,
            )
            trigger_end = min(
                period_end - M5_MS,
                setup.retest_time + 30 * 60 * 1000,
            )
            if trigger_start > trigger_end:
                continue

            first_open = _ceil_to_m5(trigger_start)
            last_open = (trigger_end // M5_MS) * M5_MS
            for trigger_open in range(first_open, last_open + 1, M5_MS):
                signal_close = trigger_open + M5_MS
                if signal_close >= period_end:
                    continue

                selected = cls._select_directional_setup(
                    frame4,
                    frame1,
                    c15_times,
                    signal_close,
                    long_setups,
                    short_setups,
                    direction_cache,
                )
                if selected is None:
                    continue
                if selected.side != setup.side:
                    continue
                if selected.bos_index != setup.bos_index:
                    continue
                if selected.retest_index != setup.retest_index:
                    continue

                candidate_map[signal_close] = selected

        return sorted(candidate_map.items(), key=lambda item: item[0])

    # -----------------------------------------------------------------------
    # Optimized 5M trigger metrics
    # -----------------------------------------------------------------------

    @staticmethod
    def _precompute_5m_metrics(
        candles: list,
    ) -> tuple[list[float], list[float], list[float]]:
        """Precompute RSI, RVOL and ATR once for the full 5M series."""
        count = len(candles)
        rsi_values = [50.0] * count
        rvol_values = [0.0] * count
        atr_values = [0.0] * count

        if count < 2:
            return rsi_values, rvol_values, atr_values

        closes = [float(c["close"]) for c in candles]
        volumes = [float(c["volume"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]

        # Exact RSI recurrence used by indicators.rsi().
        period = 14
        gains = [0.0] * (count - 1)
        losses = [0.0] * (count - 1)

        for i in range(1, count):
            change = closes[i] - closes[i - 1]
            gains[i - 1] = max(change, 0.0)
            losses[i - 1] = max(-change, 0.0)

        if count >= period + 1:
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period

            def current_rsi() -> float:
                if avg_loss == 0:
                    return 100.0
                rs = avg_gain / avg_loss
                return 100.0 - (100.0 / (1.0 + rs))

            rsi_values[period] = current_rsi()

            for i in range(period + 1, count):
                avg_gain = (
                    ((avg_gain * (period - 1)) + gains[i - 1]) / period
                )
                avg_loss = (
                    ((avg_loss * (period - 1)) + losses[i - 1]) / period
                )
                rsi_values[i] = current_rsi()

        # Exact _relative_volume(candles, lookback=20).
        lookback = 20
        if count >= lookback + 1:
            rolling = sum(volumes[:lookback])
            for i in range(lookback, count):
                average = rolling / lookback
                rvol_values[i] = volumes[i] / average if average > 0 else 0.0
                rolling += volumes[i]
                rolling -= volumes[i - lookback]

        # Exact _atr_series() values used by _five_minute_trigger().
        atr_period = 14
        true_ranges = [0.0] * count
        for i in range(1, count):
            previous_close = closes[i - 1]
            true_ranges[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - previous_close),
                abs(lows[i] - previous_close),
            )

        if count >= atr_period + 1:
            rolling_tr = sum(true_ranges[1 : atr_period + 1])
            atr_values[atr_period] = rolling_tr / atr_period
            for i in range(atr_period + 1, count):
                rolling_tr += true_ranges[i]
                rolling_tr -= true_ranges[i - atr_period]
                atr_values[i] = rolling_tr / atr_period

        return rsi_values, rvol_values, atr_values

    @staticmethod
    def _fast_trigger_ready(
        candles: list,
        index: int,
        side: str,
        setup_level: float,
        rsi_values: list[float],
        rvol_values: list[float],
    ) -> bool:
        """Exact ready/long/short condition of production _five_minute_trigger."""
        if index < 29:
            return False

        cur = candles[index]
        prev = candles[index - 1]

        open_price = float(cur["open"])
        high = float(cur["high"])
        low = float(cur["low"])
        close = float(cur["close"])

        previous_high = float(prev["high"])
        previous_low = float(prev["low"])

        rng = max(high - low, 1e-12)
        body = abs(close - open_price) / rng
        rsi_value = rsi_values[index]
        rvol_value = rvol_values[index]

        long_level = close > setup_level
        short_level = close < setup_level

        breakout_long = close > open_price and close > previous_high and long_level
        breakout_short = close < open_price and close < previous_low and short_level
        reclaim_long = close > open_price and long_level and low <= setup_level
        reclaim_short = close < open_price and short_level and high >= setup_level

        momentum_long = (
            rsi_value >= 51.0
            and rvol_value >= MIN_TRIGGER_RVOL
            and body >= MIN_TRIGGER_BODY
        )
        momentum_short = (
            rsi_value <= 49.0
            and rvol_value >= MIN_TRIGGER_RVOL
            and body >= MIN_TRIGGER_BODY
        )

        if side == "LONG":
            return (breakout_long or reclaim_long) and momentum_long
        if side == "SHORT":
            return (breakout_short or reclaim_short) and momentum_short
        return False

    # -----------------------------------------------------------------------
    # 5M candidate scan
    # -----------------------------------------------------------------------

    @classmethod
    def _find_5m_candidate_times(
        cls,
        c4: list,
        c1: list,
        c15: list,
        c5: list,
        directional_windows: list[tuple[int, _SetupCandidate]],
        period_start: int,
        period_end: int,
        setup_candidates: tuple[list[_SetupCandidate], list[_SetupCandidate]] | None = None,
    ) -> list[tuple[int, _SetupCandidate]]:
        del c4, c1, setup_candidates
        c15_times = [_row_time(row) for row in c15]
        c5_times = [_row_time(row) for row in c5]

        # The 4H/1H/15M direction was already proven in directional_windows.
        # Only 15M momentum/volume/volatility and the 5M trigger remain here.
        frame15 = _build_frame_metrics(c15)
        rsi_values, rvol_values, _atr_values = cls._precompute_5m_metrics(c5)

        candidates: dict[int, _SetupCandidate] = {}

        # Production path: inspect only the exact 5M signal closes that
        # survived 4H/1H/15M, not all ~2300 historical 5M candles.
        for signal_close, expected_setup in directional_windows:
            if signal_close < period_start or signal_close > period_end:
                continue

            trigger_open = signal_close - M5_MS
            index = bisect_right(c5_times, trigger_open) - 1
            if index < 0 or c5_times[index] != trigger_open:
                continue

            c15_index = bisect_right(c15_times, signal_close - M15_MS) - 1
            if c15_index < 0:
                continue

            if not _cheap_15m_directional_filters(
                frame15,
                c15_index,
                expected_setup.side,
            ):
                continue

            if cls._fast_trigger_ready(
                c5,
                index,
                expected_setup.side,
                expected_setup.bos_level,
                rsi_values,
                rvol_values,
            ):
                candidates[signal_close] = expected_setup

        return sorted(candidates.items(), key=lambda item: item[0])

    # -----------------------------------------------------------------------
    # Backtest symbol
    # -----------------------------------------------------------------------

    def _backtest_symbol(
        self,
        history: SymbolHistory,
        period_start: int,
        period_end: int,
        btc_history: SymbolHistory,
        btc_engine: tuple[list, list, list] | None = None,
        btc_engine_times: tuple[list[int], list[int], list[int]] | None = None,
        btc_context_cache: dict[tuple[int, int, int], tuple[dict, str]] | None = None,
    ) -> list[SimulatedTrade]:
        raw_c4 = history.candles_4h
        raw_c1 = history.candles_1h
        raw_c15 = history.candles_15m
        raw_c5 = history.candles_5m
        raw_c1d = history.candles_1d

        c4 = _convert_for_engine(raw_c4, history.symbol, "4H")
        c1 = _convert_for_engine(raw_c1, history.symbol, "1H")
        c15 = _convert_for_engine(raw_c15, history.symbol, "15M")

        if len(c4) < 205:
            raise ValueError(
                f"{history.symbol}: insufficient 4H candles ({len(c4)} < 205)"
            )
        if len(c1) < 205:
            raise ValueError(
                f"{history.symbol}: insufficient 1H candles ({len(c1)} < 205)"
            )
        if len(c15) < 80:
            raise ValueError(
                f"{history.symbol}: insufficient 15M candles ({len(c15)} < 80)"
            )

        # Compatibility path for monkeypatched/custom triggers used by the
        # existing integration tests. Production uses the optimized staged path.
        if _five_minute_trigger is not _ENGINE_FIVE_MINUTE_TRIGGER:
            return self._backtest_symbol_compatibility(
                history,
                period_start,
                period_end,
                btc_history,
                btc_engine,
            )

        if not raw_c5:
            return []
        c5 = _convert_for_engine(raw_c5, history.symbol, "5M")
        if len(c5) < 30:
            return []

        c1d = _convert_for_engine(raw_c1d, history.symbol, "1D")

        if btc_engine is None:
            btc_c4_engine = _convert_for_engine(
                btc_history.candles_4h, "BTC_USDT", "4H"
            )
            btc_c1_engine = _convert_for_engine(
                btc_history.candles_1h, "BTC_USDT", "1H"
            )
            btc_c15_engine = _convert_for_engine(
                btc_history.candles_15m, "BTC_USDT", "15M"
            )
        else:
            btc_c4_engine, btc_c1_engine, btc_c15_engine = btc_engine

        if btc_engine_times is None:
            btc_engine_times = (
                [_row_time(row) for row in btc_c4_engine],
                [_row_time(row) for row in btc_c1_engine],
                [_row_time(row) for row in btc_c15_engine],
            )
        if btc_context_cache is None:
            btc_context_cache = {}

        # The staged fetch path stores the exact trigger candidates so the
        # final engine does not redo BOS/retest/direction/5M work.
        candidate_times = list(history.prefilter_candidates)
        if not candidate_times:
            setup_candidates = self._build_setup_candidates(c15)
            directional_windows = self._find_directional_signal_windows(
                c4,
                c1,
                c15,
                period_start,
                period_end,
                setup_candidates,
            )
            candidate_times = self._find_5m_candidate_times(
                c4,
                c1,
                c15,
                c5,
                directional_windows,
                period_start,
                period_end,
                setup_candidates,
            )
            directional_count = len(directional_windows)
        else:
            directional_count = len(candidate_times)

        LOGGER.info(
            "BACKTEST CANDIDATES | %s | directional=%d trigger=%d",
            history.symbol,
            directional_count,
            len(candidate_times),
        )

        if not candidate_times:
            return []

        raw_c5_times = [_row_time(row) for row in raw_c5]
        c1d_times = [_row_time(row) for row in c1d]

        trades: list[SimulatedTrade] = []
        previous_exit_time: int | None = None

        for signal_close_time, expected_setup in candidate_times:
            if signal_close_time < period_start or signal_close_time > period_end:
                continue

            if previous_exit_time is not None and signal_close_time <= previous_exit_time:
                continue

            c4_slice = _closed_slice(c4, M4H_MS, signal_close_time)
            c1_slice = _closed_slice(c1, M1H_MS, signal_close_time)
            c15_slice = _closed_slice(c15, M15_MS, signal_close_time)
            c5_slice = _closed_slice(c5, M5_MS, signal_close_time)
            c1d_slice = _closed_slice(c1d, M1D_MS, signal_close_time)

            try:
                analysis = analyze_candles(
                    history.symbol,
                    c4_slice,
                    c1_slice,
                    c15_slice,
                    c5_slice,
                    c1d_slice,
                    now_ms=signal_close_time,
                )
            except Exception:
                LOGGER.exception(
                    "BACKTEST ENGINE failed | %s | signal=%d",
                    history.symbol,
                    signal_close_time,
                )
                continue

            if not analysis.get("technical_candidate"):
                continue

            side = str(analysis.get("setup") or "").upper()
            if side not in {"LONG", "SHORT"}:
                continue
            if side != expected_setup.side:
                continue

            symbol_upper = history.symbol.upper()
            if symbol_upper == "BTC_USDT":
                btc_ok = True
            else:
                btc_c4_end = bisect_right(
                    btc_engine_times[0],
                    signal_close_time - M4H_MS,
                )
                btc_c1_end = bisect_right(
                    btc_engine_times[1],
                    signal_close_time - M1H_MS,
                )
                btc_c15_end = bisect_right(
                    btc_engine_times[2],
                    signal_close_time - M15_MS,
                )
                cache_key = (btc_c4_end, btc_c1_end, btc_c15_end)

                cached_context = btc_context_cache.get(cache_key)
                try:
                    if cached_context is None:
                        btc_context = build_btc_context(
                            btc_c4_engine[:btc_c4_end],
                            btc_c1_engine[:btc_c1_end],
                            btc_c15_engine[:btc_c15_end],
                        )
                        btc_ok, btc_reason = btc_filter_ok(
                            side,
                            btc_context,
                            is_btc=False,
                        )
                        btc_context_cache[cache_key] = (btc_context, btc_reason)
                    else:
                        btc_context, _btc_reason = cached_context
                        btc_ok, _ = btc_filter_ok(
                            side,
                            btc_context,
                            is_btc=False,
                        )
                except Exception:
                    LOGGER.exception(
                        "BACKTEST BTC filter failed | %s | signal=%d",
                        history.symbol,
                        signal_close_time,
                    )
                    btc_ok = False

            if not btc_ok:
                continue

            future_start = bisect_right(
                raw_c5_times,
                signal_close_time - 1,
            )
            future_end = bisect_right(
                raw_c5_times,
                period_end - 1,
            )
            if future_start >= future_end:
                continue

            future_candles = raw_c5[future_start:future_end]
            if not future_candles:
                continue

            try:
                trade = simulate_trade(
                    analysis,
                    future_candles,
                    signal_close_time_ms=signal_close_time,
                )
            except Exception:
                LOGGER.exception(
                    "BACKTEST SIMULATION failed | %s | signal=%d",
                    history.symbol,
                    signal_close_time,
                )
                continue

            if trade is not None:
                trades.append(trade)
                previous_exit_time = trade.exit_time_ms

        return trades

    # -----------------------------------------------------------------------
    # Compatibility implementation for patched trigger tests
    # -----------------------------------------------------------------------

    def _backtest_symbol_compatibility(
        self,
        history: SymbolHistory,
        period_start: int,
        period_end: int,
        btc_history: SymbolHistory,
        btc_engine: tuple[list, list, list] | None = None,
    ) -> list[SimulatedTrade]:
        raw_c4 = history.candles_4h
        raw_c1 = history.candles_1h
        raw_c15 = history.candles_15m
        raw_c5 = history.candles_5m
        raw_c1d = history.candles_1d

        c4 = _convert_for_engine(raw_c4, history.symbol, "4H")
        c1 = _convert_for_engine(raw_c1, history.symbol, "1H")
        c15 = _convert_for_engine(raw_c15, history.symbol, "15M")
        c5 = _convert_for_engine(raw_c5, history.symbol, "5M")
        c1d = _convert_for_engine(raw_c1d, history.symbol, "1D")

        if len(c4) < 205:
            raise ValueError(
                f"{history.symbol}: insufficient 4H candles ({len(c4)} < 205)"
            )
        if len(c1) < 205:
            raise ValueError(
                f"{history.symbol}: insufficient 1H candles ({len(c1)} < 205)"
            )
        if len(c15) < 80:
            raise ValueError(
                f"{history.symbol}: insufficient 15M candles ({len(c15)} < 80)"
            )
        if len(c5) < 30:
            raise ValueError(
                f"{history.symbol}: insufficient 5M candles ({len(c5)} < 30)"
            )

        if btc_engine is None:
            btc_c4_engine = _convert_for_engine(
                btc_history.candles_4h, "BTC_USDT", "4H"
            )
            btc_c1_engine = _convert_for_engine(
                btc_history.candles_1h, "BTC_USDT", "1H"
            )
            btc_c15_engine = _convert_for_engine(
                btc_history.candles_15m, "BTC_USDT", "15M"
            )
        else:
            btc_c4_engine, btc_c1_engine, btc_c15_engine = btc_engine

        c4_times = [_row_time(row) for row in c4]
        c1_times = [_row_time(row) for row in c1]
        c15_times = [_row_time(row) for row in c15]
        c5_times = [_row_time(row) for row in c5]
        btc4_times = [_row_time(row) for row in btc_c4_engine]
        btc1_times = [_row_time(row) for row in btc_c1_engine]
        btc15_times = [_row_time(row) for row in btc_c15_engine]
        raw_c5_times = [_row_time(row) for row in raw_c5]

        rsi_values, rvol_values, _atr_values = self._precompute_5m_metrics(c5)

        candidate_times: set[int] = set()

        for side in ("LONG", "SHORT"):
            try:
                bos_events = _bos_events(
                    c15,
                    side,
                    lookback=len(c15),
                )
            except Exception:
                LOGGER.exception(
                    "BACKTEST BOS failed | %s | side=%s",
                    history.symbol,
                    side,
                )
                continue

            for bos in bos_events:
                try:
                    retest = _pullback_retest(
                        c15,
                        side,
                        bos,
                        MAX_SETUP_AGE_15M,
                    )
                except Exception:
                    LOGGER.exception(
                        "BACKTEST RETEST failed | %s | side=%s",
                        history.symbol,
                        side,
                    )
                    continue

                if not retest.get("valid"):
                    continue

                try:
                    retest_time = int(retest["time"])
                    bos_time = int(bos["time"])
                    setup_level = float(bos["level"])
                except (TypeError, ValueError, KeyError):
                    continue

                if retest_time + M15_MS < period_start:
                    continue
                if retest_time > period_end:
                    continue
                if bos_time > retest_time:
                    continue

                trigger_start = max(period_start, retest_time + M15_MS)
                trigger_end = min(
                    period_end - M5_MS,
                    retest_time + 30 * 60 * 1000,
                )
                if trigger_start > trigger_end:
                    continue

                first_index = bisect_right(c5_times, trigger_start - 1)
                last_index = bisect_right(c5_times, trigger_end)

                for index in range(first_index, last_index):
                    trigger_open = c5_times[index]
                    trigger_close = trigger_open + M5_MS
                    if trigger_close > period_end or index < 29:
                        continue

                    try:
                        trigger = _five_minute_trigger(
                            c5[: index + 1],
                            side,
                            setup_level,
                        )
                    except Exception:
                        LOGGER.exception(
                            "BACKTEST TRIGGER failed | %s | side=%s | index=%d",
                            history.symbol,
                            side,
                            index,
                        )
                        continue

                    if trigger.get("ready"):
                        candidate_times.add(trigger_close)

        if not candidate_times:
            return []

        trades: list[SimulatedTrade] = []
        previous_exit_time: int | None = None

        for signal_close_time in sorted(candidate_times):
            if signal_close_time < period_start or signal_close_time > period_end:
                continue
            if previous_exit_time is not None and signal_close_time <= previous_exit_time:
                continue

            c4_slice = _closed_slice(c4, M4H_MS, signal_close_time)
            c1_slice = _closed_slice(c1, M1H_MS, signal_close_time)
            c15_slice = _closed_slice(c15, M15_MS, signal_close_time)
            c5_slice = _closed_slice(c5, M5_MS, signal_close_time)
            c1d_slice = _closed_slice(c1d, M1D_MS, signal_close_time)

            try:
                analysis = analyze_candles(
                    history.symbol,
                    c4_slice,
                    c1_slice,
                    c15_slice,
                    c5_slice,
                    c1d_slice,
                    now_ms=signal_close_time,
                )
            except Exception:
                LOGGER.exception(
                    "BACKTEST ENGINE failed | %s | signal=%d",
                    history.symbol,
                    signal_close_time,
                )
                continue

            if not analysis.get("technical_candidate"):
                continue

            side = str(analysis.get("setup") or "").upper()
            if side not in {"LONG", "SHORT"}:
                continue

            if history.symbol.upper() == "BTC_USDT":
                btc_ok = True
            else:
                btc_c4_end = bisect_right(
                    btc4_times,
                    signal_close_time - M4H_MS,
                )
                btc_c1_end = bisect_right(
                    btc1_times,
                    signal_close_time - M1H_MS,
                )
                btc_c15_end = bisect_right(
                    btc15_times,
                    signal_close_time - M15_MS,
                )

                try:
                    btc_context = build_btc_context(
                        btc_c4_engine[:btc_c4_end],
                        btc_c1_engine[:btc_c1_end],
                        btc_c15_engine[:btc_c15_end],
                    )
                    btc_ok, _ = btc_filter_ok(
                        side,
                        btc_context,
                        is_btc=False,
                    )
                except Exception:
                    LOGGER.exception(
                        "BACKTEST BTC filter failed | %s | signal=%d",
                        history.symbol,
                        signal_close_time,
                    )
                    btc_ok = False

            if not btc_ok:
                continue

            future_start = bisect_right(raw_c5_times, signal_close_time - 1)
            future_end = bisect_right(raw_c5_times, period_end - 1)
            if future_start >= future_end:
                continue

            future_candles = raw_c5[future_start:future_end]
            try:
                trade = simulate_trade(
                    analysis,
                    future_candles,
                    signal_close_time_ms=signal_close_time,
                )
            except Exception:
                LOGGER.exception(
                    "BACKTEST SIMULATION failed | %s | signal=%d",
                    history.symbol,
                    signal_close_time,
                )
                continue

            if trade is not None:
                trades.append(trade)
                previous_exit_time = trade.exit_time_ms

        return trades

    # -----------------------------------------------------------------------
    # Full-history API retained for compatibility / tests
    # -----------------------------------------------------------------------

    async def _fetch_symbol_history(
        self,
        symbol: str,
        period_start: int,
        period_end: int,
    ) -> SymbolHistory:
        starts = {
            "4h": period_start - MIN_4H_WARMUP_MS,
            "1h": period_start - MIN_1H_WARMUP_MS,
            "15m": period_start - MIN_15M_WARMUP_MS,
            "5m": period_start - MIN_5M_WARMUP_MS,
            "1d": period_start - MIN_1D_WARMUP_MS,
        }

        tasks = [
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "4H",
                    INTERVALS["4h"],
                    starts["4h"],
                    period_end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "1H",
                    INTERVALS["1h"],
                    starts["1h"],
                    period_end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "15M",
                    INTERVALS["15m"],
                    starts["15m"],
                    period_end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "5M",
                    INTERVALS["5m"],
                    starts["5m"],
                    period_end,
                )
            ),
            asyncio.create_task(
                _fetch_timeframe(
                    self.client,
                    symbol,
                    "1D",
                    INTERVALS["1d"],
                    starts["1d"],
                    period_end,
                )
            ),
        ]

        try:
            c4h, c1h, c15m, c5m, c1d = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception as exc:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise RuntimeError(
                f"{symbol}: historical data fetch failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        return SymbolHistory(
            symbol=symbol,
            candles_4h=c4h,
            candles_1h=c1h,
            candles_15m=c15m,
            candles_5m=c5m,
            candles_1d=c1d,
        )

    async def _fetch_btc_history(
        self,
        period_start: int,
        period_end: int,
    ) -> SymbolHistory:
        return await self._fetch_symbol_history(
            "BTC_USDT",
            period_start,
            period_end,
        )
