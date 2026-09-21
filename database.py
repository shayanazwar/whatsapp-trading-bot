from __future__ import annotations

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


class Database:
    def __init__(self, path: str = "bot.sqlite3") -> None:
        self.path = Path(path)
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
        """Return True only the first time a message id is seen."""
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
