from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]


class CandleDataError(ValueError):
    pass


def validate_candles(frame: pd.DataFrame, interval_hours: int = 1, require_contiguous: bool = False) -> pd.DataFrame:
    missing = set(["timestamp", *OHLCV]) - set(frame.columns)
    if missing:
        raise CandleDataError(f"Missing candle columns: {sorted(missing)}")
    out = frame[["timestamp", *OHLCV]].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    out = out.dropna(subset=["timestamp", *OHLCV]).sort_values("timestamp").reset_index(drop=True)
    if out.timestamp.duplicated().any():
        raise CandleDataError("Duplicate candle timestamps")
    if (out["high"] < out[["open", "close", "low"]].max(axis=1)).any():
        raise CandleDataError("OHLC invalid: high is below another price field")
    if (out["low"] > out[["open", "close", "high"]].min(axis=1)).any():
        raise CandleDataError("OHLC invalid: low is above another price field")
    if (out["volume"] < 0).any() or (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise CandleDataError("Non-positive price or negative volume")
    interval = pd.Timedelta(hours=interval_hours)
    if not (out.timestamp.dt.floor(interval) == out.timestamp).all():
        raise CandleDataError(f"Timestamps are not aligned to {interval_hours}H UTC boundaries")
    if require_contiguous and len(out) > 1:
        deltas = out.timestamp.diff().dropna()
        if not (deltas == interval).all():
            raise CandleDataError(f"Missing or non-contiguous {interval_hours}H candles")
    return out


def build_bars_from_1h(hourly: pd.DataFrame, hours: int, strict: bool = True) -> pd.DataFrame:
    """Aggregate completed 1H candles into UTC-aligned bars of N hours.

    Each output is retained only when exactly N contiguous one-hour children exist.
    12H therefore always consists of three completed, validated 4H bars if called
    through build_timeframes() below.
    """
    if hours not in {4, 12, 24}:
        raise ValueError("Only 4H, 12H and 1D aggregate bars are supported")
    source = validate_candles(hourly, interval_hours=1, require_contiguous=False)
    if source.empty:
        return pd.DataFrame(columns=["timestamp", *OHLCV])
    epoch_hours = source.timestamp.astype("int64") // 3_600_000_000_000
    bucket = (epoch_hours // hours) * hours
    work = source.copy()
    work["bucket"] = pd.to_datetime(bucket * 3_600_000_000_000, utc=True)
    rows: list[dict[str, object]] = []
    for start, group in work.groupby("bucket", sort=True):
        group = group.sort_values("timestamp")
        expected = pd.date_range(start=start, periods=hours, freq="1h", tz="UTC")
        if len(group) != hours or list(group.timestamp) != list(expected):
            continue
        rows.append(
            {
                "timestamp": start,
                "open": float(group.iloc[0].open),
                "high": float(group.high.max()),
                "low": float(group.low.min()),
                "close": float(group.iloc[-1].close),
                "volume": float(group.volume.sum()),
            }
        )
    result = pd.DataFrame(rows, columns=["timestamp", *OHLCV])
    if strict and not result.empty:
        return validate_candles(result, interval_hours=hours, require_contiguous=False)
    return result


def build_timeframes(hourly: pd.DataFrame) -> dict[str, pd.DataFrame]:
    one_hour = validate_candles(hourly, interval_hours=1, require_contiguous=False)
    four_hour = build_bars_from_1h(one_hour, 4)
    # 12H candles are built only from three contiguous completed 4H candles.
    twelve_rows: list[dict[str, object]] = []
    if not four_hour.empty:
        work = four_hour.copy()
        epoch_hours = work.timestamp.astype("int64") // 3_600_000_000_000
        work["bucket"] = pd.to_datetime(((epoch_hours // 12) * 12) * 3_600_000_000_000, utc=True)
        for start, group in work.groupby("bucket", sort=True):
            group = group.sort_values("timestamp")
            expected = pd.date_range(start=start, periods=3, freq="4h", tz="UTC")
            if len(group) != 3 or list(group.timestamp) != list(expected):
                continue
            twelve_rows.append(
                {
                    "timestamp": start,
                    "open": float(group.iloc[0].open),
                    "high": float(group.high.max()),
                    "low": float(group.low.min()),
                    "close": float(group.iloc[-1].close),
                    "volume": float(group.volume.sum()),
                }
            )
    twelve_hour = pd.DataFrame(twelve_rows, columns=["timestamp", *OHLCV])
    one_day = build_bars_from_1h(one_hour, 24)
    return {"1H": one_hour, "4H": four_hour, "12H": twelve_hour, "1D": one_day}
