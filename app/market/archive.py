"""Worker-local append-only gzip candle archive.

The archive stores raw 1M source candles and derived 3M execution candles. PostgreSQL
remains the shared source of truth for scan/job/signal metadata; only the worker owns this
persistent disk. Files are partitioned by timeframe, symbol and UTC date.
"""
from __future__ import annotations

import gzip
import json
import os
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from app.market.candles import Candle
from app.symbols import normalize_symbol


_ARCHIVABLE_TIMEFRAMES = {"1M", "3M"}


class CandleArchive:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._latest: dict[str, int] = {}
        # Cache the timestamp/value index for only recent shards. This avoids
        # decompressing a full daily gzip file on every 3-minute append.
        self._known_by_shard: dict[tuple[str, str, str], dict[int, Candle]] = {}
        self._index_path = self.root / "latest_candles.json"
        self._legacy_index_path = self.root / "latest_1m.json"
        self._load_index()

    @staticmethod
    def _index_key(symbol: str, timeframe: str) -> str:
        return f"{timeframe}:{symbol}"

    def _load_index(self) -> None:
        try:
            index_source = self._index_path if self._index_path.is_file() else self._legacy_index_path
            payload = json.loads(index_source.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if ":" in str(key):
                        self._latest[str(key)] = int(value)
                    else:
                        # Backward compatibility with the first 1M-only archive.
                        self._latest[self._index_key(normalize_symbol(str(key)), "1M")] = int(value)
        except (OSError, ValueError, TypeError):
            self._latest = {}

    def _save_index(self) -> None:
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._latest, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._index_path)

    @staticmethod
    def _day(ts: int) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")

    def _path(self, symbol: str, ts: int, timeframe: str) -> Path:
        safe = normalize_symbol(symbol)
        tf = timeframe.lower()
        path = self.root / tf / safe / f"{self._day(ts)}.jsonl.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def latest_open_time(self, symbol: str, timeframe: str = "1M") -> int | None:
        tf = timeframe.upper()
        if tf not in _ARCHIVABLE_TIMEFRAMES:
            raise ValueError(f"Unsupported archive timeframe: {tf}")
        return self._latest.get(self._index_key(normalize_symbol(symbol), tf))

    def _read_shard(self, symbol: str, timeframe: str, day: str, path: Path) -> dict[int, Candle]:
        key = (symbol, timeframe, day)
        cached = self._known_by_shard.get(key)
        if cached is not None:
            return cached
        existing: dict[int, Candle] = {}
        if path.is_file():
            with gzip.open(path, "rt", encoding="utf-8") as reader:
                for line in reader:
                    try:
                        row = json.loads(line)
                        bar = Candle(**{k: row[k] for k in Candle.__dataclass_fields__ if k in row})
                        if bar.symbol != symbol or bar.timeframe != timeframe:
                            raise ValueError("Archive shard contains a different symbol/timeframe")
                        previous = existing.get(bar.open_time)
                        if previous is not None and previous != bar:
                            raise ValueError(f"Conflicting archived candle for {symbol} at {bar.open_time}")
                        existing[bar.open_time] = bar
                    except (json.JSONDecodeError, KeyError, TypeError) as exc:
                        raise ValueError(f"Corrupt market archive file: {path.name}") from exc
        self._known_by_shard[key] = existing
        return existing

    def append(self, candles: Iterable[Candle]) -> int:
        grouped: dict[tuple[str, str, str], list[Candle]] = defaultdict(list)
        for candle in candles:
            timeframe = candle.timeframe.upper()
            if timeframe not in _ARCHIVABLE_TIMEFRAMES:
                raise ValueError("CandleArchive accepts only 1M source or 3M derived candles")
            grouped[(normalize_symbol(candle.symbol), timeframe, self._day(candle.open_time))].append(candle)
        written = 0
        with self._lock:
            today = datetime.now(timezone.utc).date()
            for (symbol, timeframe, day), bars in grouped.items():
                path = self._path(symbol, bars[0].open_time, timeframe)
                existing = self._read_shard(symbol, timeframe, day, path)
                new_bars: list[Candle] = []
                for bar in sorted(bars, key=lambda c: c.open_time):
                    previous = existing.get(bar.open_time)
                    if previous is not None:
                        comparable = (
                            previous.symbol, previous.timeframe, previous.open_time,
                            previous.open, previous.high, previous.low, previous.close,
                            previous.volume, previous.amount, previous.real_open,
                            previous.real_high, previous.real_low, previous.real_close,
                            previous.complete,
                        )
                        candidate = (
                            bar.symbol, bar.timeframe, bar.open_time, bar.open, bar.high,
                            bar.low, bar.close, bar.volume, bar.amount, bar.real_open,
                            bar.real_high, bar.real_low, bar.real_close, bar.complete,
                        )
                        if comparable != candidate:
                            raise ValueError(f"Conflicting archived {timeframe} candle for {symbol} at {bar.open_time}")
                        continue
                    existing[bar.open_time] = bar
                    new_bars.append(bar)
                if new_bars:
                    with gzip.open(path, "at", encoding="utf-8") as handle:
                        for bar in new_bars:
                            record = {
                                "symbol": bar.symbol, "timeframe": bar.timeframe,
                                "open_time": bar.open_time, "open": bar.open, "high": bar.high,
                                "low": bar.low, "close": bar.close, "volume": bar.volume,
                                "amount": bar.amount, "real_open": bar.real_open,
                                "real_high": bar.real_high, "real_low": bar.real_low,
                                "real_close": bar.real_close, "complete": bar.complete,
                                "source": bar.source,
                            }
                            handle.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
                            written += 1
                    latest_key = self._index_key(symbol, timeframe)
                    self._latest[latest_key] = max(self._latest.get(latest_key, 0), max(b.open_time for b in new_bars))
            if written:
                # Keep only today and yesterday's shards hot. Each symbol/day shard
                # can contain 1,440 1M Candle objects; retaining 7+ days across 100
                # symbols creates an avoidable long-lived worker memory footprint.
                # Older shards are safely re-indexed from gzip when later appended.
                cutoff_day = today - timedelta(days=1)
                self._known_by_shard = {
                    key: value for key, value in self._known_by_shard.items()
                    if date.fromisoformat(key[2]) >= cutoff_day
                }
                self._save_index()
        return written

    def load(self, symbol: str, start: int, end: int, timeframe: str = "1M") -> list[Candle]:
        symbol = normalize_symbol(symbol)
        tf = timeframe.upper()
        if tf not in _ARCHIVABLE_TIMEFRAMES:
            raise ValueError(f"Unsupported archive timeframe: {tf}")
        if end < start:
            raise ValueError("end must be >= start")
        first_day = datetime.fromtimestamp(start, timezone.utc).date()
        last_day = datetime.fromtimestamp(end, timezone.utc).date()
        by_time: dict[int, Candle] = {}
        with self._lock:
            current = first_day
            while current <= last_day:
                path = self.root / tf.lower() / symbol / f"{current.isoformat()}.jsonl.gz"
                if path.is_file():
                    with gzip.open(path, "rt", encoding="utf-8") as handle:
                        for line in handle:
                            try:
                                row = json.loads(line)
                                stamp = int(row["open_time"])
                                if start <= stamp <= end:
                                    bar = Candle(**{k: row[k] for k in Candle.__dataclass_fields__ if k in row})
                                    if bar.symbol != symbol or bar.timeframe != tf:
                                        raise ValueError("Archive row identity mismatch")
                                    previous = by_time.get(stamp)
                                    if previous is not None and previous != bar:
                                        raise ValueError(f"Conflicting archived candle for {symbol} at {stamp}")
                                    by_time[stamp] = bar
                            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                                raise ValueError(f"Corrupt market archive file: {path.name}") from exc
                current += timedelta(days=1)
        return [by_time[t] for t in sorted(by_time)]

    def coverage(self, symbol: str, start: int, end: int, timeframe: str = "1M") -> dict[str, int | float | None]:
        bars = self.load(symbol, start, end, timeframe=timeframe)
        step = 60 if timeframe.upper() == "1M" else 180
        first_open = ((int(start) + step - 1) // step) * step
        last_open = (int(end) // step) * step
        expected = max(0, ((last_open - first_open) // step) + 1) if last_open >= first_open else 0
        coverage = min(100.0, len(bars) / expected * 100.0) if expected else 0.0
        return {
            "candles": len(bars), "expected": expected, "coverage_pct": coverage,
            "first_open_time": bars[0].open_time if bars else None,
            "last_open_time": bars[-1].open_time if bars else None,
        }
