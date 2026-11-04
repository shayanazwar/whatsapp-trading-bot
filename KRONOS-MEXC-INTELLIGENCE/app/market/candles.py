from __future__ import annotations

import pandas as pd

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "amount"]
TIMEFRAME_SECONDS = {"Min60": 3600, "Hour4": 14400, "Day1": 86400}


def validate_candles(frame: pd.DataFrame, *, deduplicate: bool = True, reject_duplicates: bool = False) -> pd.DataFrame:
    missing = set(CANDLE_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing candle fields: {sorted(missing)}")
    out = frame[CANDLE_COLUMNS].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="raise")
    for column in CANDLE_COLUMNS[1:]:
        out[column] = pd.to_numeric(out[column], errors="raise").astype(float)
    out = out.sort_values("timestamp")
    if out["timestamp"].duplicated().any():
        if reject_duplicates or not deduplicate:
            duplicates = out.loc[out["timestamp"].duplicated(keep=False), "timestamp"].astype(str).tolist()[:5]
            raise ValueError(f"Duplicate candle timestamps detected: {duplicates}")
        out = out.drop_duplicates("timestamp", keep="last")
    if out.empty:
        raise ValueError("No candle data")
    if not (out[["open", "high", "low", "close"]] > 0).all().all():
        raise ValueError("OHLC prices must be positive")
    if (out["high"] < out[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError("Invalid candle high")
    if (out["low"] > out[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("Invalid candle low")
    if (out[["volume", "amount"]] < 0).any().any():
        raise ValueError("Volume and amount cannot be negative")
    return out.reset_index(drop=True)


def closed_candles(frame: pd.DataFrame, timeframe_seconds: int, as_of=None) -> pd.DataFrame:
    out = validate_candles(frame)
    as_of = pd.Timestamp(as_of or pd.Timestamp.now(tz="UTC"))
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")
    else:
        as_of = as_of.tz_convert("UTC")
    return out.loc[out["timestamp"] + pd.to_timedelta(timeframe_seconds, unit="s") <= as_of].reset_index(drop=True)


def aggregate_ohlcv(frame: pd.DataFrame, rule: str, expected_count: int) -> pd.DataFrame:
    """Aggregate only exact, contiguous source candle groups aligned to UTC epoch."""
    data = validate_candles(frame).set_index("timestamp")
    rule_delta = pd.Timedelta(rule)
    rows = []
    for start, group in data.groupby(pd.Grouper(freq=rule, origin="epoch")):
        if len(group) != expected_count:
            continue
        expected = pd.date_range(start, periods=expected_count, freq=(rule_delta / expected_count), tz="UTC")
        if not group.index.equals(expected):
            continue
        rows.append({
            "timestamp": start,
            "open": float(group.iloc[0]["open"]),
            "high": float(group["high"].max()),
            "low": float(group["low"].min()),
            "close": float(group.iloc[-1]["close"]),
            "volume": float(group["volume"].sum()),
            "amount": float(group["amount"].sum()),
        })
    return pd.DataFrame(rows, columns=CANDLE_COLUMNS)


def aggregate_12h_from_4h(frame: pd.DataFrame) -> pd.DataFrame:
    return aggregate_ohlcv(frame, "12h", 3)


def aggregate_4h_from_1h(frame: pd.DataFrame) -> pd.DataFrame:
    return aggregate_ohlcv(frame, "4h", 4)


def validate_candle_continuity(frame: pd.DataFrame, timeframe_seconds: int, *,
                               require_utc_alignment: bool = True) -> pd.DataFrame:
    """Validate a complete, equally spaced candle series without silently deduplicating."""
    if timeframe_seconds <= 0:
        raise ValueError("timeframe_seconds must be positive")
    out = validate_candles(frame, deduplicate=False, reject_duplicates=True)
    seconds = out["timestamp"].astype("int64") // 1_000_000_000
    if require_utc_alignment and (seconds % timeframe_seconds != 0).any():
        raise ValueError(f"Candle timestamps are not aligned to {timeframe_seconds}s UTC boundaries")
    diffs = seconds.diff().dropna()
    if not diffs.empty and (diffs != timeframe_seconds).any():
        bad = diffs[diffs != timeframe_seconds]
        first_index = int(bad.index[0])
        actual = int(bad.iloc[0])
        raise ValueError(
            f"Candle series is not contiguous at row {first_index}: expected {timeframe_seconds}s, got {actual}s"
        )
    return out
