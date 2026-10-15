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

                CREATE TABLE IF NOT EXISTS paper_wallet (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_balance REAL NOT NULL,
                    balance REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('LONG', 'SHORT')),
                    entry_price REAL NOT NULL,
                    mark_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    margin REAL NOT NULL,
                    notional REAL NOT NULL,
                    quantity REAL NOT NULL,
                    entry_fee REAL NOT NULL DEFAULT 0,
                    exit_fee REAL NOT NULL DEFAULT 0,
                    gross_pnl REAL NOT NULL DEFAULT 0,
                    net_pnl REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
                    exit_price REAL,
                    exit_reason TEXT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_trades_status
                ON paper_trades(status, opened_at);
                CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol
                ON paper_trades(symbol, status);
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

    def is_message_seen(self, message_id: str) -> bool:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ? LIMIT 1",
                (message_id,),
            ).fetchone()
        return row is not None

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


    # ------------------------------------------------------------------
    # Persistent virtual-money paper trading
    # ------------------------------------------------------------------

    def ensure_paper_wallet(self, initial_balance: float) -> dict:
        """Create the paper wallet once; never reset it during a deploy/restart."""
        now = datetime.now(timezone.utc).isoformat()
        initial = float(initial_balance)
        if initial <= 0:
            raise ValueError("Paper initial balance must be positive")
        with self.lock, self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO paper_wallet(id, initial_balance, balance, created_at, updated_at)
                   VALUES (1, ?, ?, ?, ?)""",
                (initial, initial, now, now),
            )
            row = conn.execute("SELECT * FROM paper_wallet WHERE id = 1").fetchone()
        return dict(row)

    def get_paper_wallet(self) -> dict | None:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM paper_wallet WHERE id = 1").fetchone()
        return dict(row) if row else None

    def create_paper_trade(
        self,
        *,
        signal_key: str,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        leverage: int,
        margin: float,
        notional: float,
        quantity: float,
        entry_fee: float,
        max_open_trades: int,
        opened_at: str,
    ) -> tuple[str, dict | None]:
        """Atomically open a virtual trade and charge its entry fee."""
        side = str(side).upper()
        if side not in {"LONG", "SHORT"}:
            raise ValueError("Paper trade side must be LONG or SHORT")
        values = (entry_price, stop_loss, take_profit, margin, notional, quantity)
        if not all(float(value) > 0 for value in values):
            raise ValueError("Paper trade prices and sizes must be positive")
        if (side == "LONG" and not stop_loss < entry_price < take_profit) or (
            side == "SHORT" and not take_profit < entry_price < stop_loss
        ):
            raise ValueError("Paper trade entry/SL/TP geometry is invalid")
        with self.lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM paper_trades WHERE signal_key = ?", (signal_key,)
            ).fetchone()
            if existing:
                return "DUPLICATE", dict(existing)
            wallet = conn.execute("SELECT * FROM paper_wallet WHERE id = 1").fetchone()
            if not wallet:
                raise RuntimeError("Paper wallet is not initialized")
            open_rows = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(margin), 0) AS used FROM paper_trades WHERE status = 'OPEN'"
            ).fetchone()
            if int(open_rows["n"]) >= max(1, int(max_open_trades)):
                return "MAX_OPEN_TRADES", None
            available = float(wallet["balance"]) - float(open_rows["used"])
            if margin > available + 1e-9 or float(wallet["balance"]) <= 0:
                return "INSUFFICIENT_BALANCE", None
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                """INSERT INTO paper_trades(
                    signal_key, symbol, side, entry_price, mark_price, stop_loss, take_profit,
                    leverage, margin, notional, quantity, entry_fee, status, opened_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)""",
                (signal_key, symbol.upper(), side, float(entry_price), float(entry_price),
                 float(stop_loss), float(take_profit), int(leverage), float(margin),
                 float(notional), float(quantity), max(0.0, float(entry_fee)), opened_at, now),
            )
            conn.execute(
                "UPDATE paper_wallet SET balance = balance - ?, updated_at = ? WHERE id = 1",
                (max(0.0, float(entry_fee)), now),
            )
            row = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return "OPENED", dict(row)

    def list_paper_trades(self, status: str | None = None, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        sql = "SELECT * FROM paper_trades"
        params: list[object] = []
        if status:
            sql += " WHERE status = ?"
            params.append(str(status).upper())
        sql += " ORDER BY opened_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_paper_realized_pnl(self) -> float:
        """Return realized net PnL for the full paper ledger, not only recent rows."""
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(net_pnl), 0) AS pnl FROM paper_trades WHERE status = 'CLOSED'"
            ).fetchone()
        return float(row["pnl"] or 0.0)

    def update_paper_mark(self, trade_id: int, mark_price: float) -> bool:
        if float(mark_price) <= 0:
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE paper_trades SET mark_price = ?, updated_at = ? WHERE id = ? AND status = 'OPEN'",
                (float(mark_price), now, int(trade_id)),
            )
        return cursor.rowcount == 1

    def close_paper_trade(
        self,
        *,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        gross_pnl: float,
        exit_fee: float,
        net_pnl: float,
        closed_at: str,
    ) -> dict | None:
        """Close once and settle gross PnL less the exit fee into virtual cash."""
        if float(exit_price) <= 0:
            raise ValueError("Paper exit price must be positive")
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE id = ? AND status = 'OPEN'", (int(trade_id),)
            ).fetchone()
            if not row:
                return None
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """UPDATE paper_trades SET mark_price = ?, exit_price = ?, exit_reason = ?,
                   gross_pnl = ?, exit_fee = ?, net_pnl = ?, status = 'CLOSED', closed_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'OPEN'""",
                (float(exit_price), float(exit_price), str(exit_reason), float(gross_pnl),
                 max(0.0, float(exit_fee)), float(net_pnl), closed_at, now, int(trade_id)),
            )
            conn.execute(
                "UPDATE paper_wallet SET balance = balance + ?, updated_at = ? WHERE id = 1",
                (float(gross_pnl) - max(0.0, float(exit_fee)), now),
            )
            updated = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (int(trade_id),)).fetchone()
        return dict(updated)
