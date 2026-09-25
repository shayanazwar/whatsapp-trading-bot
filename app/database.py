from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional


@dataclass(frozen=True)
class Alert:
    id: int
    phone: str
    exchange: str
    symbol: str
    condition: str
    target: float
    active: bool
    created_at: str


@dataclass(frozen=True)
class SignalRecord:
    signal_key: str
    symbol: str
    side: str
    candle_time: int
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    rr: float
    confluence: int
    status: str
    analysis_json: str
    created_at: str
    expires_at: str
    updated_at: str


class Database:
    def __init__(self, path: str = "signals.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self.lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    condition TEXT NOT NULL CHECK(condition IN ('above', 'below')),
                    target REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_active_phone
                ON alerts(phone, active);

                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    signal_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT')),
                    candle_time INTEGER NOT NULL,
                    entry REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    rr REAL NOT NULL,
                    confluence INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    analysis_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_signals_symbol_candle
                ON signals(symbol, candle_time);
                CREATE INDEX IF NOT EXISTS idx_signals_status
                ON signals(status);
                """
            )

    def create_alert(
        self, phone: str, exchange: str, symbol: str, condition: str, target: float
    ) -> Alert:
        created = datetime.now(timezone.utc).isoformat()
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO alerts(phone, exchange, symbol, condition, target, active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (phone, exchange, symbol, condition, target, created),
            )
            row = conn.execute("SELECT * FROM alerts WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._to_alert(row)

    def list_alerts(self, phone: str, active_only: bool = True) -> list[Alert]:
        sql = "SELECT * FROM alerts WHERE phone = ?"
        params: list[object] = [phone]
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY id DESC"
        with self.lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._to_alert(row) for row in rows]

    def get_alert(self, alert_id: int, phone: str) -> Optional[Alert]:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id = ? AND phone = ?", (alert_id, phone)
            ).fetchone()
        return self._to_alert(row) if row else None

    def deactivate(self, alert_id: int, phone: str) -> bool:
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE alerts SET active = 0 WHERE id = ? AND phone = ? AND active = 1",
                (alert_id, phone),
            )
            return cursor.rowcount == 1

    def deactivate_all(self, phone: str) -> int:
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE alerts SET active = 0 WHERE phone = ? AND active = 1", (phone,)
            )
            return cursor.rowcount

    def mark_message_seen(self, message_id: str) -> bool:
        processed = datetime.now(timezone.utc).isoformat()
        with self.lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO processed_messages(message_id, processed_at) VALUES (?, ?)",
                    (message_id, processed),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def create_signal_if_new(
        self,
        *,
        signal_key: str,
        symbol: str,
        side: str,
        candle_time: int,
        entry: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        rr: float,
        confluence: int,
        analysis_json: str,
        created_at: str,
        expires_at: str,
    ) -> bool:
        with self.lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO signals(
                        signal_key, symbol, side, candle_time, entry, stop_loss,
                        tp1, tp2, rr, confluence, status, analysis_json,
                        created_at, expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?, ?, ?, ?)
                    """,
                    (
                        signal_key,
                        symbol,
                        side,
                        candle_time,
                        entry,
                        stop_loss,
                        tp1,
                        tp2,
                        rr,
                        confluence,
                        analysis_json,
                        created_at,
                        expires_at,
                        created_at,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def update_signal_status(self, signal_key: str, status: str) -> bool:
        updated = datetime.now(timezone.utc).isoformat()
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE signals SET status = ?, updated_at = ? WHERE signal_key = ?",
                (status, updated, signal_key),
            )
            return cursor.rowcount == 1

    def get_signal(self, signal_key: str) -> Optional[SignalRecord]:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE signal_key = ?", (signal_key,)
            ).fetchone()
        return self._to_signal(row) if row else None


    def get_last_signal_for_symbol_side(self, symbol: str, side: str) -> Optional[SignalRecord]:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE symbol = ? AND side = ? ORDER BY created_at DESC LIMIT 1",
                (symbol.upper(), side.upper()),
            ).fetchone()
        return self._to_signal(row) if row else None

    def list_recent_signals(self, limit: int = 100) -> list[SignalRecord]:
        limit = max(1, min(limit, 500))
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._to_signal(row) for row in rows]

    @staticmethod
    def _to_alert(row: sqlite3.Row) -> Alert:
        return Alert(
            id=int(row["id"]),
            phone=str(row["phone"]),
            exchange=str(row["exchange"]),
            symbol=str(row["symbol"]),
            condition=str(row["condition"]),
            target=float(row["target"]),
            active=bool(row["active"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _to_signal(row: sqlite3.Row) -> SignalRecord:
        return SignalRecord(
            signal_key=str(row["signal_key"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            candle_time=int(row["candle_time"]),
            entry=float(row["entry"]),
            stop_loss=float(row["stop_loss"]),
            tp1=float(row["tp1"]),
            tp2=float(row["tp2"]),
            rr=float(row["rr"]),
            confluence=int(row["confluence"]),
            status=str(row["status"]),
            analysis_json=str(row["analysis_json"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            updated_at=str(row["updated_at"]),
        )
