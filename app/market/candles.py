"""Canonical candle model, parsing, validation and deterministic 12H aggregation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Iterable, Mapping
from datetime import datetime, timezone
import calendar

# Tokens are deliberately unambiguous: 3M means three minutes; 1MO means one month.
INTERVAL_SECONDS: dict[str, int] = {
    "1M": 60,
    "3M": 3 * 60,
    "5M": 5 * 60,
    "15M": 15 * 60,
    "30M": 30 * 60,
    "1H": 3600,
    "4H": 4 * 3600,
    "8H": 8 * 3600,
    "12H": 12 * 3600,
    "1D": 24 * 3600,
    "3D": 3 * 24 * 3600,
    "1W": 7 * 24 * 3600,
    # Approximation for paging only; Candle.close_time handles calendar-month boundaries.
    "1MO": 30 * 24 * 3600,
}
STRATEGY_TIMEFRAMES = frozenset({"1D", "12H", "4H", "1H"})


class CandleDataError(ValueError):
    pass


def normalize_timestamp(value: Any) -> int:
    """Return epoch seconds. MEXC docs specify seconds; accept ms defensively by magnitude."""
    try:
        ts = int(float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandleDataError(f"Invalid candle timestamp: {value!r}") from exc
    if ts >= 100_000_000_000:  # milliseconds
        ts //= 1000
    if ts <= 0:
        raise CandleDataError(f"Invalid candle timestamp: {value!r}")
    return ts


def _num(value: Any, label: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandleDataError(f"Invalid {label}: {value!r}") from exc
    if not isfinite(result):
        raise CandleDataError(f"Non-finite {label}")
    return result


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    timeframe: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float | None = None
    real_open: float | None = None
    real_high: float | None = None
    real_low: float | None = None
    real_close: float | None = None
    complete: bool = True
    source: str = "mexc_rest"

    @property
    def duration(self) -> int:
        try:
            return INTERVAL_SECONDS[self.timeframe]
        except KeyError as exc:
            raise CandleDataError(f"Unsupported timeframe: {self.timeframe}") from exc

    @property
    def close_time(self) -> int:
        if self.timeframe == "1MO":
            dt = datetime.fromtimestamp(self.open_time, tz=timezone.utc)
            year = dt.year + (1 if dt.month == 12 else 0)
            month = 1 if dt.month == 12 else dt.month + 1
            return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
        return self.open_time + self.duration

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["close_time"] = self.close_time
        return row


def _field(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return default


def parse_kline_payload(payload: Mapping[str, Any], symbol: str, timeframe: str) -> list[Candle]:
    """Parse MEXC array-of-fields payloads and row-based adapters without guessing fields silently."""
    tf = timeframe.upper()
    if tf not in INTERVAL_SECONDS:
        raise CandleDataError(f"Unsupported timeframe: {timeframe}")
    if payload.get("success") is False:
        raise CandleDataError(f"MEXC returned success=false, code={payload.get('code')}")
    code = payload.get("code")
    if code not in (None, 0, "0", 200, "200") and payload.get("success") is not True:
        raise CandleDataError(f"MEXC returned error code={code}")
    data = payload.get("data", payload)
    rows: list[dict[str, Any]] = []
    if isinstance(data, Mapping):
        times = _field(data, "time", "timestamp", "openTime", "open_time")
        if not isinstance(times, (list, tuple)):
            if any(k in data for k in ("openTime", "open_time", "time")):
                rows = [dict(data)]
            else:
                raise CandleDataError("Kline data object lacks a time array")
        else:
            keys = {
                "open": ("open", "o"), "high": ("high", "h"), "low": ("low", "l"),
                "close": ("close", "c"), "volume": ("vol", "volume", "v"),
                "amount": ("amount", "quoteVolume", "q"),
                "real_open": ("realOpen", "real_open", "ro"),
                "real_high": ("realHigh", "real_high", "rh"),
                "real_low": ("realLow", "real_low", "rl"),
                "real_close": ("realClose", "real_close", "rc"),
            }
            for i, ts in enumerate(times):
                row: dict[str, Any] = {"time": ts}
                for out_key, aliases in keys.items():
                    value = _field(data, *aliases)
                    if isinstance(value, (list, tuple)) and i < len(value):
                        row[out_key] = value[i]
                rows.append(row)
    elif isinstance(data, (list, tuple)):
        for item in data:
            if isinstance(item, Mapping):
                rows.append(dict(item))
            elif isinstance(item, (list, tuple)) and len(item) >= 6:
                # Conventional row order: time, open, high, low, close, volume[, amount]
                row = {"time": item[0], "open": item[1], "high": item[2], "low": item[3], "close": item[4], "vol": item[5]}
                if len(item) > 6:
                    row["amount"] = item[6]
                rows.append(row)
            else:
                raise CandleDataError("Unsupported kline row shape")
    else:
        raise CandleDataError("Kline payload data must be an object or list")

    result: list[Candle] = []
    for row in rows:
        ts_value = _field(row, "time", "timestamp", "openTime", "open_time")
        op = _num(_field(row, "open", "o"), "open")
        hi = _num(_field(row, "high", "h"), "high")
        lo = _num(_field(row, "low", "l"), "low")
        cl = _num(_field(row, "close", "c"), "close")
        vol = _num(_field(row, "vol", "volume", "v", default=0), "volume")
        amount = _num(_field(row, "amount", "quoteVolume", "q"), "amount", optional=True)
        assert op is not None and hi is not None and lo is not None and cl is not None and vol is not None
        candle = Candle(
            symbol=symbol,
            timeframe=tf,
            open_time=normalize_timestamp(ts_value),
            open=op, high=hi, low=lo, close=cl, volume=vol, amount=amount,
            real_open=_num(_field(row, "real_open", "realOpen", "ro"), "real_open", optional=True),
            real_high=_num(_field(row, "real_high", "realHigh", "rh"), "real_high", optional=True),
            real_low=_num(_field(row, "real_low", "realLow", "rl"), "real_low", optional=True),
            real_close=_num(_field(row, "real_close", "realClose", "rc"), "real_close", optional=True),
            source="mexc_rest",
        )
        validate_candle(candle)
        result.append(candle)
    return sorted(result, key=lambda c: c.open_time)


def validate_candle(candle: Candle) -> None:
    vals = (candle.open, candle.high, candle.low, candle.close, candle.volume)
    if any(not isfinite(x) for x in vals):
        raise CandleDataError("Candle has non-finite values")
    if min(candle.open, candle.high, candle.low, candle.close) <= 0:
        raise CandleDataError("Candle prices must be positive")
    if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
        raise CandleDataError("OHLC invariants failed")
    if candle.high < candle.low or candle.volume < 0:
        raise CandleDataError("High/low or volume invariant failed")


def closed_candles(candles: Iterable[Candle], now_epoch: int, grace_seconds: int = 5) -> list[Candle]:
    """Return complete bars whose candle interval ended before now minus grace."""
    if grace_seconds < 0:
        raise ValueError("grace_seconds cannot be negative")
    out: list[Candle] = []
    seen: set[int] = set()
    for candle in sorted(candles, key=lambda c: c.open_time):
        validate_candle(candle)
        if candle.open_time in seen:
            raise CandleDataError(f"Duplicate candle open time {candle.open_time}")
        seen.add(candle.open_time)
        if candle.close_time <= now_epoch - grace_seconds:
            out.append(candle)
    return out


def validate_contiguous(candles: list[Candle], expected_timeframe: str | None = None) -> None:
    if not candles:
        raise CandleDataError("No candles supplied")
    tf = expected_timeframe or candles[0].timeframe
    duration = INTERVAL_SECONDS.get(tf)
    if duration is None:
        raise CandleDataError(f"Unsupported timeframe: {tf}")
    seen: set[int] = set()
    for c in candles:
        if c.timeframe != tf:
            raise CandleDataError("Mixed timeframe candle set")
        if c.open_time in seen:
            raise CandleDataError("Duplicate candle open time")
        seen.add(c.open_time)
    for a, b in zip(candles, candles[1:]):
        if b.open_time - a.open_time != duration:
            raise CandleDataError(f"Candle gap/overlap between {a.open_time} and {b.open_time}")


def aggregate_12h_from_4h(candles: Iterable[Candle]) -> list[Candle]:
    """Aggregate only three contiguous, completed UTC-aligned 4H bars per 12H window."""
    bars = sorted((c for c in candles if c.timeframe == "4H" and c.complete), key=lambda c: c.open_time)
    if not bars:
        return []
    by_time: dict[int, Candle] = {}
    for candle in bars:
        validate_candle(candle)
        if candle.open_time in by_time:
            raise CandleDataError(f"Duplicate 4H candle at {candle.open_time}")
        by_time[candle.open_time] = candle
    grouped: list[Candle] = []
    twelve_hours = INTERVAL_SECONDS["12H"]
    four_hours = INTERVAL_SECONDS["4H"]
    starts = sorted(by_time)
    for start in starts:
        if start % twelve_hours != 0:
            continue
        chunk = [by_time.get(start + i * four_hours) for i in range(3)]
        if any(c is None for c in chunk):
            continue
        bars3 = [c for c in chunk if c is not None]
        if len({c.symbol for c in bars3}) != 1:
            continue
        if not all(c.complete for c in bars3):
            continue
        amount = None if any(c.amount is None for c in bars3) else sum(c.amount or 0 for c in bars3)
        real_available = all(c.real_open is not None and c.real_high is not None and c.real_low is not None and c.real_close is not None for c in bars3)
        aggregated = Candle(
            symbol=bars3[0].symbol,
            timeframe="12H",
            open_time=start,
            open=bars3[0].open,
            high=max(c.high for c in bars3),
            low=min(c.low for c in bars3),
            close=bars3[-1].close,
            volume=sum(c.volume for c in bars3),
            amount=amount,
            real_open=bars3[0].real_open if real_available else None,
            real_high=max(c.real_high for c in bars3) if real_available else None,
            real_low=min(c.real_low for c in bars3) if real_available else None,
            real_close=bars3[-1].real_close if real_available else None,
            complete=True,
            source="derived_12h_from_4h",
        )
        validate_candle(aggregated)
        grouped.append(aggregated)
    return grouped


def assert_strategy_timeframes(timeframes: Iterable[str]) -> None:
    invalid = set(timeframes) - STRATEGY_TIMEFRAMES
    if invalid:
        raise CandleDataError(f"Forbidden strategy timeframe(s): {', '.join(sorted(invalid))}")


def _aggregate_exact_groups(candles: Iterable[Candle], source_tf: str, target_tf: str,
                           source_step: int, target_step: int) -> list[Candle]:
    """Build a larger bar only from a full, contiguous, UTC/epoch-aligned set."""
    bars = sorted((c for c in candles if c.timeframe == source_tf and c.complete), key=lambda c: c.open_time)
    by_time: dict[int, Candle] = {}
    for candle in bars:
        validate_candle(candle)
        if candle.open_time in by_time:
            raise CandleDataError(f"Duplicate {source_tf} candle at {candle.open_time}")
        by_time[candle.open_time] = candle
    out: list[Candle] = []
    for start in sorted(by_time):
        if start % target_step != 0:
            continue
        group = [by_time.get(start + offset * source_step) for offset in range(target_step // source_step)]
        if any(c is None for c in group):
            continue
        real = [c for c in group if c is not None]
        if len({c.symbol for c in real}) != 1 or not all(c.complete for c in real):
            continue
        if any(b.open_time - a.open_time != source_step for a, b in zip(real, real[1:])):
            continue
        amount = None if any(c.amount is None for c in real) else sum(c.amount or 0.0 for c in real)
        real_available = all(all(v is not None for v in (c.real_open, c.real_high, c.real_low, c.real_close)) for c in real)
        combined = Candle(
            symbol=real[0].symbol, timeframe=target_tf, open_time=start,
            open=real[0].open, high=max(c.high for c in real), low=min(c.low for c in real),
            close=real[-1].close, volume=sum(c.volume for c in real), amount=amount,
            real_open=real[0].real_open if real_available else None,
            real_high=max(float(c.real_high) for c in real) if real_available else None,
            real_low=min(float(c.real_low) for c in real) if real_available else None,
            real_close=real[-1].real_close if real_available else None,
            complete=True, source=f"derived_{target_tf.lower()}_from_{source_tf.lower()}",
        )
        validate_candle(combined)
        out.append(combined)
    return out

def aggregate_3m_from_1m(candles: Iterable[Candle]) -> list[Candle]:
    """Build 3-minute bars from exactly three complete, contiguous UTC-aligned 1-minute bars."""
    return _aggregate_exact_groups(candles, "1M", "3M", 60, 180)

def aggregate_3d_from_1d(candles: Iterable[Candle]) -> list[Candle]:
    """Build 3-day display bars from three contiguous UTC-aligned daily bars."""
    return _aggregate_exact_groups(candles, "1D", "3D", 86400, 3 * 86400)
