from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    processed_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sent_signals (
                    signal_key TEXT PRIMARY KEY,
                    sent_at INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    candle_time INTEGER NOT NULL
                );
            """)

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set(self, key: str, value: Any, now: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, str(value), now),
            )

    def mark_message_processed(self, message_id: str, now: int) -> bool:
        if not message_id:
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_messages(message_id,processed_at) VALUES(?,?)",
                (message_id, now),
            )
            return cur.rowcount == 1

    def mark_signal_sent(self, signal_key: str, direction: str, candle_time: int, now: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO sent_signals(signal_key,sent_at,direction,candle_time) VALUES(?,?,?,?)",
                (signal_key, now, direction, candle_time),
            )
            return cur.rowcount == 1
