"""SQLite persistence with append-only events, idempotent inbox and durable outbox."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class StoreError(RuntimeError):
    pass


class _PostgresConnectionAdapter:
    """Minimal SQLite-placeholder adapter for the SQL shared by both backends."""
    def __init__(self, connection: Any) -> None:
        self.raw = connection

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        return self.raw.execute(sql.replace("?", "%s"), parameters)


class Store:
    def __init__(self, database_path: str | Path) -> None:
        raw = str(database_path)
        self.database_url = raw if raw.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")) else ""
        if self.database_url:
            self.path = Path("./data/mexc_bot.sqlite3")  # only a local fallback path; not opened for PostgreSQL
        else:
            self.path = Path(database_path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        if self.database_url:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise StoreError("DATABASE_URL requires the pinned psycopg[binary] dependency") from exc
            conn = None
            try:
                conn = psycopg.connect(self.database_url.replace("postgresql+psycopg://", "postgresql://", 1),
                                      connect_timeout=10, row_factory=dict_row)
                yield _PostgresConnectionAdapter(conn)
                conn.commit()
            except Exception as exc:
                if conn is not None:
                    conn.rollback()
                if exc.__class__.__module__.startswith("psycopg"):
                    raise StoreError(f"Database operation failed: {type(exc).__name__}") from exc
                raise
            finally:
                if conn is not None:
                    conn.close()
            return

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.path, timeout=15.0, isolation_level="IMMEDIATE")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.rollback()
            raise StoreError(f"Database operation failed: {type(exc).__name__}") from exc
        finally:
            if conn is not None:
                conn.close()

    @property
    def database_fingerprint(self) -> str:
        """Return a non-secret identity for checking that web and worker share storage."""
        identity = self.database_url or str(self.path.resolve())
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    def _initialize(self) -> None:
        if self.database_url:
            self._initialize_postgres()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=15) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                  id TEXT PRIMARY KEY, event_type TEXT NOT NULL, symbol TEXT,
                  created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                  payload_sha256 TEXT NOT NULL, idempotency_key TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

                CREATE TABLE IF NOT EXISTS inbox (
                  message_id TEXT PRIMARY KEY, sender TEXT NOT NULL, body TEXT,
                  received_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                  processed INTEGER NOT NULL DEFAULT 0, process_note TEXT
                );

                CREATE TABLE IF NOT EXISTS outbox (
                  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                  category TEXT NOT NULL, recipient TEXT NOT NULL, body TEXT NOT NULL,
                  created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, delivered_at TEXT, provider_message_id TEXT, expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, created_at);

                CREATE TABLE IF NOT EXISTS alerts (
                  id TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL,
                  params_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1, last_state_json TEXT,
                  last_trigger_at TEXT, last_trigger_key TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active, symbol);

                CREATE TABLE IF NOT EXISTS paper_positions (
                  id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                  entry_time INTEGER NOT NULL, entry_price REAL NOT NULL,
                  stop_price REAL NOT NULL, target_price REAL NOT NULL,
                  quantity REAL NOT NULL, mode TEXT NOT NULL,
                  meta_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                  exit_time INTEGER, exit_price REAL, realised_pnl REAL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status, symbol);

                CREATE TABLE IF NOT EXISTS paper_intents (
                  candidate_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                  decision_time INTEGER NOT NULL, expected_entry_time INTEGER NOT NULL,
                  candidate_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending', rejection_reason TEXT, paper_position_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_paper_intents_status ON paper_intents(status, expected_entry_time);

                CREATE TABLE IF NOT EXISTS app_state (
                  state_key TEXT PRIMARY KEY, state_value TEXT NOT NULL, updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS research_runs (
                  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL,
                  config_json TEXT NOT NULL, result_json TEXT, status TEXT NOT NULL,
                  error TEXT
                );
                CREATE TABLE IF NOT EXISTS signals (
                  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                  symbol TEXT NOT NULL, direction TEXT NOT NULL, candle_close_time INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL, score REAL NOT NULL, strategy_version TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'active', payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL, invalidation_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_signals_active ON signals(status, expires_at, score);

                CREATE TABLE IF NOT EXISTS backtest_jobs (
                  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                  recipient TEXT NOT NULL, args_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'queued', worker_id TEXT,
                  started_at TEXT, finished_at TEXT, result_json TEXT, error TEXT,
                  progress_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_backtest_jobs_status ON backtest_jobs(status, created_at);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)").fetchall()}
            if "provider_message_id" not in columns:
                conn.execute("ALTER TABLE outbox ADD COLUMN provider_message_id TEXT")
            if "expires_at" not in columns:
                conn.execute("ALTER TABLE outbox ADD COLUMN expires_at TEXT")
            job_columns = {row[1] for row in conn.execute("PRAGMA table_info(backtest_jobs)").fetchall()}
            if "progress_json" not in job_columns:
                conn.execute("ALTER TABLE backtest_jobs ADD COLUMN progress_json TEXT")
            conn.commit()

    def _initialize_postgres(self) -> None:
        statements = [
            "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, event_type TEXT NOT NULL, symbol TEXT, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, idempotency_key TEXT UNIQUE)",
            "CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)",
            "CREATE TABLE IF NOT EXISTS inbox (message_id TEXT PRIMARY KEY, sender TEXT NOT NULL, body TEXT, received_at TEXT NOT NULL, payload_json TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0, process_note TEXT)",
            "CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, category TEXT NOT NULL, recipient TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, delivered_at TEXT, provider_message_id TEXT, expires_at TEXT)",
            "CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, created_at)",
            "CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL, params_json TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, last_state_json TEXT, last_trigger_at TEXT, last_trigger_key TEXT)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active, symbol)",
            "CREATE TABLE IF NOT EXISTS paper_positions (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL, entry_time BIGINT NOT NULL, entry_price DOUBLE PRECISION NOT NULL, stop_price DOUBLE PRECISION NOT NULL, target_price DOUBLE PRECISION NOT NULL, quantity DOUBLE PRECISION NOT NULL, mode TEXT NOT NULL, meta_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', exit_time BIGINT, exit_price DOUBLE PRECISION, realised_pnl DOUBLE PRECISION)",
            "CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status, symbol)",
            "CREATE TABLE IF NOT EXISTS paper_intents (candidate_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL, decision_time BIGINT NOT NULL, expected_entry_time BIGINT NOT NULL, candidate_json TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', rejection_reason TEXT, paper_position_id TEXT)",
            "CREATE INDEX IF NOT EXISTS idx_paper_intents_status ON paper_intents(status, expected_entry_time)",
            "CREATE TABLE IF NOT EXISTS app_state (state_key TEXT PRIMARY KEY, state_value TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS research_runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL, config_json TEXT NOT NULL, result_json TEXT, status TEXT NOT NULL, error TEXT)",
            "CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL, direction TEXT NOT NULL, candle_close_time BIGINT NOT NULL, expires_at BIGINT NOT NULL, score DOUBLE PRECISION NOT NULL, strategy_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, invalidation_reason TEXT)",
            "CREATE INDEX IF NOT EXISTS idx_signals_active ON signals(status, expires_at, score)",
            "CREATE TABLE IF NOT EXISTS backtest_jobs (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, recipient TEXT NOT NULL, args_json TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', worker_id TEXT, started_at TEXT, finished_at TEXT, result_json TEXT, error TEXT, progress_json TEXT)",
            "CREATE INDEX IF NOT EXISTS idx_backtest_jobs_status ON backtest_jobs(status, created_at)",
            "ALTER TABLE backtest_jobs ADD COLUMN IF NOT EXISTS progress_json TEXT",
            "ALTER TABLE outbox ADD COLUMN IF NOT EXISTS provider_message_id TEXT",
            "ALTER TABLE outbox ADD COLUMN IF NOT EXISTS expires_at TEXT",
        ]
        with self.connection() as conn:
            for statement in statements:
                conn.execute(statement)

    def append_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        symbol: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        payload_json = canonical_json(dict(payload))
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        event_id = str(uuid.uuid4())
        with self.connection() as conn:
            if idempotency_key:
                existing = conn.execute("SELECT id FROM events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if existing:
                    return str(existing["id"])
            conn.execute(
                "INSERT INTO events(id,event_type,symbol,created_at,payload_json,payload_sha256,idempotency_key) VALUES(?,?,?,?,?,?,?)",
                (event_id, event_type, symbol, utc_now_iso(), payload_json, digest, idempotency_key),
            )
        return event_id

    def recent_events(self, limit: int = 100, event_type: str | None = None) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 1000)
        with self.connection() as conn:
            if event_type:
                rows = conn.execute(
                    "SELECT * FROM events WHERE event_type=? ORDER BY created_at DESC LIMIT ?", (event_type, limit)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_inbound(self, message_id: str, sender: str, body: str, payload: Mapping[str, Any]) -> bool:
        if not message_id:
            raise ValueError("message_id is required for idempotency")
        with self.connection() as conn:
            cur = conn.execute(
                "INSERT INTO inbox(message_id,sender,body,received_at,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(message_id) DO NOTHING",
                (message_id, sender, body, utc_now_iso(), canonical_json(dict(payload))),
            )
            return cur.rowcount == 1

    def pending_inbound(
        self, limit: int = 50, *, after: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return a stable keyset page of pending inbound messages.

        The cursor remains valid while earlier rows are asynchronously processed,
        unlike OFFSET pagination over a changing ``processed=0`` result set.
        """
        bounded = min(max(int(limit), 1), 500)
        with self.connection() as conn:
            if after is None:
                rows = conn.execute(
                    "SELECT * FROM inbox WHERE processed=0 ORDER BY received_at,message_id LIMIT ?",
                    (bounded,),
                ).fetchall()
            else:
                received_at, message_id = after
                rows = conn.execute(
                    "SELECT * FROM inbox WHERE processed=0 AND "
                    "(received_at>? OR (received_at=? AND message_id>?)) "
                    "ORDER BY received_at,message_id LIMIT ?",
                    (received_at, received_at, message_id, bounded),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_inbound(self, message_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM inbox WHERE message_id=?", (message_id,)).fetchone()
        return dict(row) if row is not None else None

    def mark_inbound_processed(self, message_id: str, note: str = "processed") -> None:
        with self.connection() as conn:
            conn.execute("UPDATE inbox SET processed=1, process_note=? WHERE message_id=?", (note[:500], message_id))

    def enqueue_outbox(
        self, recipient: str, body: str, *, category: str = "service", idempotency_key: str,
        expires_at: str | None = None,
    ) -> str:
        outbox_id = str(uuid.uuid4())
        with self.connection() as conn:
            row = conn.execute("SELECT id FROM outbox WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                return str(row["id"])
            conn.execute(
                "INSERT INTO outbox(id,idempotency_key,category,recipient,body,created_at,status,expires_at) VALUES(?,?,?,?,?,?, 'pending', ?)",
                (outbox_id, idempotency_key, category, recipient, body, utc_now_iso(), expires_at),
            )
        return outbox_id

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        now = utc_now_iso()
        with self.connection() as conn:
            conn.execute("UPDATE outbox SET status='expired', last_error='signal TTL elapsed' WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<=?", (now,))
            rows = conn.execute(
                "SELECT * FROM outbox WHERE status='pending' AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at LIMIT ?",
                (now, min(max(limit, 1), 500)),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_outbox_attempt(self, outbox_id: str, *, error: str | None = None, delivered: bool = False,
                              accepted: bool = False, provider_message_id: str | None = None) -> None:
        status = "delivered" if delivered else "accepted" if accepted else "pending"
        with self.connection() as conn:
            conn.execute(
                "UPDATE outbox SET attempts=attempts+1, status=?, last_error=?, delivered_at=?, provider_message_id=COALESCE(?, provider_message_id) WHERE id=?",
                (status, (error or "")[:500] or None, utc_now_iso() if delivered else None, provider_message_id, outbox_id),
            )

    def record_outbox_status(
        self, provider_message_id: str, status: str, error: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        allowed = {"sent", "delivered", "read", "failed"}
        if status not in allowed:
            return False
        local_status = "delivered" if status in {"delivered", "read"} else "failed" if status == "failed" else "accepted"
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE outbox SET status=?, last_error=?, delivered_at=CASE WHEN ?='delivered' THEN COALESCE(delivered_at, ?) ELSE delivered_at END WHERE provider_message_id=?",
                (local_status, (error or "")[:500] or None, local_status, utc_now_iso(), provider_message_id),
            )
            if cur.rowcount == 1:
                conn.execute(
                    "INSERT INTO events(id,event_type,created_at,payload_json,payload_sha256,idempotency_key) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(idempotency_key) DO NOTHING",
                    (str(uuid.uuid4()), f"WHATSAPP_STATUS_{status.upper()}", utc_now_iso(),
                     canonical_json({"provider_message_id": provider_message_id, "status": status, "error": error, "details": dict(details or {})}),
                     hashlib.sha256(canonical_json({"provider_message_id": provider_message_id, "status": status, "error": error, "details": dict(details or {})}).encode()).hexdigest(),
                     f"wa-status:{provider_message_id}:{status}"),
                )
                return True
            return False

    def outbox_month_summary(self, month_prefix: str | None = None) -> dict[str, int]:
        """Count locally tracked outbound messages in the current UTC calendar month."""
        if month_prefix is None:
            month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM outbox WHERE substr(created_at,1,7)=? GROUP BY status",
                (month_prefix,),
            ).fetchall()
        result = {"pending": 0, "accepted": 0, "delivered": 0, "failed": 0, "unknown": 0}
        for row in rows:
            if row["status"] in result:
                result[str(row["status"])] = int(row["n"])
        result["total"] = sum(result.values())
        return result

    def add_paper_intent(self, candidate: Mapping[str, Any], *, expected_entry_time: int, setup_key: str) -> bool:
        candidate_id = str(candidate["candidate_id"])
        with self.connection() as conn:
            cur = conn.execute(
                "INSERT INTO paper_intents(candidate_id,symbol,side,decision_time,expected_entry_time,candidate_json,created_at,status) VALUES(?,?,?,?,?,?,?,'pending') ON CONFLICT(candidate_id) DO NOTHING",
                (candidate_id, candidate["symbol"], candidate["side"], int(candidate["decision_time"]),
                 int(expected_entry_time), canonical_json({**dict(candidate), "setup_key": setup_key}), utc_now_iso()),
            )
            return cur.rowcount == 1

    def paper_intents(self, *, pending_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM paper_intents" + (" WHERE status='pending'" if pending_only else "") + " ORDER BY expected_entry_time LIMIT ?"
        with self.connection() as conn:
            rows = conn.execute(query, (min(max(int(limit), 1), 1000),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["candidate"] = json.loads(item.pop("candidate_json"))
            out.append(item)
        return out

    def update_paper_intent(self, candidate_id: str, *, status: str, rejection_reason: str | None = None,
                            paper_position_id: str | None = None) -> bool:
        if status not in {"pending", "filled_simulated", "rejected_simulated", "cancelled_simulated"}:
            raise ValueError("Invalid paper intent status")
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE paper_intents SET status=?, rejection_reason=?, paper_position_id=? WHERE candidate_id=? AND status='pending'",
                (status, (rejection_reason or "")[:300] or None, paper_position_id, candidate_id),
            )
            return cur.rowcount == 1

    def update_paper_position_meta(self, position_id: str, meta: Mapping[str, Any]) -> bool:
        with self.connection() as conn:
            cur = conn.execute("UPDATE paper_positions SET meta_json=? WHERE id=? AND status='open'",
                               (canonical_json(dict(meta)), position_id))
            return cur.rowcount == 1

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT state_value FROM app_state WHERE state_key=?", (key,)).fetchone()
        return str(row["state_value"]) if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.connection() as conn:
            conn.execute("INSERT INTO app_state(state_key,state_value,updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=excluded.updated_at",
                         (key, value, utc_now_iso()))

    def create_alert(self, symbol: str, kind: str, params: Mapping[str, Any], alert_id: str | None = None) -> str:
        alert_id = alert_id or uuid.uuid4().hex[:10].upper()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO alerts(id,symbol,kind,params_json,created_at) VALUES(?,?,?,?,?)",
                (alert_id, symbol, kind, canonical_json(dict(params)), utc_now_iso()),
            )
        return alert_id

    def list_alerts(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM alerts" + (" WHERE active=1" if active_only else "") + " ORDER BY created_at"
        with self.connection() as conn:
            rows = conn.execute(query).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item.pop("params_json"))
            item["last_state"] = json.loads(item.pop("last_state_json")) if item.get("last_state_json") else None
            output.append(item)
        return output

    def update_alert(
        self,
        alert_id: str,
        *,
        active: bool | None = None,
        last_state: Mapping[str, Any] | None = None,
        trigger_at: str | None = None,
        trigger_key: str | None = None,
    ) -> bool:
        updates: list[str] = []
        values: list[Any] = []
        if active is not None:
            updates.append("active=?"); values.append(1 if active else 0)
        if last_state is not None:
            updates.append("last_state_json=?"); values.append(canonical_json(dict(last_state)))
        if trigger_at is not None:
            updates.append("last_trigger_at=?"); values.append(trigger_at)
        if trigger_key is not None:
            updates.append("last_trigger_key=?"); values.append(trigger_key)
        if not updates:
            return False
        values.append(alert_id)
        with self.connection() as conn:
            cur = conn.execute(f"UPDATE alerts SET {', '.join(updates)} WHERE id=?", values)
            return cur.rowcount == 1

    def delete_alert(self, alert_id: str) -> bool:
        with self.connection() as conn:
            cur = conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
            return cur.rowcount == 1

    def save_signal(self, payload: Mapping[str, Any], *, strategy_version: str) -> str:
        """Persist a signal once per symbol/direction/candle/strategy version."""
        symbol = str(payload["symbol"]).upper()
        direction = str(payload["direction"]).upper()
        candle_time = int(payload["timestamp"])
        expires_at = int(payload["expires_at"])
        score = float(payload.get("score", 0.0))
        key = f"{symbol}:{direction}:{candle_time}:{strategy_version}"
        signal_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        now = utc_now_iso()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO signals(id,idempotency_key,symbol,direction,candle_close_time,expires_at,score,strategy_version,status,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,'active',?,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
                (signal_id, key, symbol, direction, candle_time, expires_at, score, strategy_version,
                 canonical_json(dict(payload)), now, now),
            )
            row = conn.execute("SELECT id FROM signals WHERE idempotency_key=?", (key,)).fetchone()
        return str(row["id"]) if row else signal_id

    def active_signals(self, *, now_epoch: int, limit: int = 50,
                       strategy_version: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            conn.execute(
                "UPDATE signals SET status='expired',updated_at=? WHERE status='active' AND expires_at<=?",
                (utc_now_iso(), int(now_epoch)),
            )
            query = "SELECT * FROM signals WHERE status='active' AND expires_at>?"
            params: list[Any] = [int(now_epoch)]
            if strategy_version:
                query += " AND strategy_version=?"
                params.append(str(strategy_version))
            query += " ORDER BY candle_close_time DESC LIMIT ?"
            params.append(min(max(int(limit), 1), 200))
            rows = conn.execute(query, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def update_signal_status(self, signal_id: str, *, status: str, reason: str | None = None) -> bool:
        allowed = {"active", "expired", "invalidated", "triggered", "closed"}
        if status not in allowed:
            raise ValueError("Invalid signal lifecycle status")
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE signals SET status=?,invalidation_reason=?,updated_at=? WHERE id=? AND status='active'",
                (status, (reason or "")[:300] or None, utc_now_iso(), signal_id),
            )
            return cur.rowcount == 1

    def enqueue_backtest_job(self, recipient: str, args: Mapping[str, Any], *, idempotency_key: str) -> str:
        """Create a durable backtest job; repeated webhook delivery returns the same job ID."""
        if not idempotency_key:
            raise ValueError("Backtest job idempotency_key is required")
        job_id = str(uuid.uuid4())
        with self.connection() as conn:
            # Insert-first + unique constraint is atomic across concurrent Postgres
            # webhook retries. A SELECT-then-INSERT sequence can race and enqueue twice.
            conn.execute(
                "INSERT INTO backtest_jobs(id,idempotency_key,recipient,args_json,created_at,status) "
                "VALUES(?,?,?,?,?,'queued') ON CONFLICT(idempotency_key) DO NOTHING",
                (job_id, idempotency_key, recipient or "", canonical_json(dict(args)), utc_now_iso()),
            )
            existing = conn.execute(
                "SELECT id FROM backtest_jobs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
        if not existing:
            raise StoreError("Backtest job insert was not persisted")
        return str(existing["id"])

    def claim_next_backtest_job(self, worker_id: str) -> dict[str, Any] | None:
        """Atomically lease one queued job. Postgres uses row locking; SQLite uses IMMEDIATE."""
        with self.connection() as conn:
            if self.database_url:
                row = conn.execute(
                    "SELECT * FROM backtest_jobs WHERE status='queued' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE backtest_jobs SET status='running',worker_id=?,started_at=? WHERE id=? AND status='queued'",
                    (worker_id, utc_now_iso(), row["id"]),
                )
            else:
                row = conn.execute(
                    "SELECT * FROM backtest_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                updated = conn.execute(
                    "UPDATE backtest_jobs SET status='running',worker_id=?,started_at=? WHERE id=? AND status='queued'",
                    (worker_id, utc_now_iso(), row["id"]),
                )
                if updated.rowcount != 1:
                    return None
            result = dict(row)
            result["status"] = "running"
            result["worker_id"] = worker_id
            result["args"] = json.loads(result.pop("args_json"))
            return result

    def requeue_interrupted_backtest_jobs(self) -> int:
        """Recover jobs left RUNNING after a worker crash/redeploy.

        Call only after the single-worker PostgreSQL advisory lock has been acquired.
        The worker is then the exclusive consumer, so no live job is stolen.
        """
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE backtest_jobs SET status='queued', worker_id=NULL, started_at=NULL, "
                "error='Requeued after worker restart', progress_json=? WHERE status='running'",
                (canonical_json({"stage": "recovered_after_restart", "recovered_at": utc_now_iso()}),),
            )
            return int(cur.rowcount)

    def update_backtest_job_progress(self, job_id: str, progress: Mapping[str, Any]) -> bool:
        """Persist bounded job progress without storing large intermediate market data."""
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE backtest_jobs SET progress_json=? WHERE id=? AND status='running'",
                (canonical_json(dict(progress)), job_id),
            )
            return cur.rowcount == 1

    def finish_backtest_job(
        self, job_id: str, *, result: Mapping[str, Any] | None = None, error: str | None = None,
    ) -> bool:
        """Finish a running job once; a later notification error cannot erase success."""
        finished_at = utc_now_iso()
        with self.connection() as conn:
            if error:
                cur = conn.execute(
                    "UPDATE backtest_jobs SET status='failed',finished_at=?,error=?,progress_json=? "
                    "WHERE id=? AND status!='complete'",
                    (finished_at, error[:2000], canonical_json({"stage": "failed", "finished_at": finished_at}), job_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE backtest_jobs SET status='complete',finished_at=?,result_json=?,error=NULL,progress_json=? "
                    "WHERE id=? AND status='running'",
                    (finished_at, canonical_json(dict(result or {})),
                     canonical_json({"stage": "completed", "finished_at": finished_at}), job_id),
                )
            return cur.rowcount == 1

    def get_backtest_job(self, job_id: str, *, recipient: str | None = None) -> dict[str, Any] | None:
        with self.connection() as conn:
            if recipient:
                row = conn.execute("SELECT * FROM backtest_jobs WHERE id=? AND recipient=?", (job_id, recipient)).fetchone()
            else:
                row = conn.execute("SELECT * FROM backtest_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["args"] = json.loads(result.pop("args_json"))
        if result.get("result_json"):
            result["result"] = json.loads(result.pop("result_json"))
        if result.get("progress_json"):
            result["progress"] = json.loads(result.pop("progress_json"))
        else:
            result.pop("progress_json", None)
        return result

    def recent_backtest_jobs(self, limit: int = 10, *, recipient: str | None = None) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 100)
        with self.connection() as conn:
            if recipient:
                rows = conn.execute(
                    "SELECT * FROM backtest_jobs WHERE recipient=? ORDER BY created_at DESC LIMIT ?",
                    (recipient, bounded),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM backtest_jobs ORDER BY created_at DESC LIMIT ?", (bounded,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["args"] = json.loads(item.pop("args_json"))
            if item.get("result_json"):
                item["result"] = json.loads(item.pop("result_json"))
            else:
                item.pop("result_json", None)
            if item.get("progress_json"):
                item["progress"] = json.loads(item.pop("progress_json"))
            else:
                item.pop("progress_json", None)
            output.append(item)
        return output

    def create_research_run(self, mode: str, config: Mapping[str, Any], run_id: str | None = None) -> str:
        run_id = run_id or str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO research_runs(id,created_at,mode,config_json,status) VALUES(?,?,?,?, 'running')",
                (run_id, utc_now_iso(), mode, canonical_json(dict(config))),
            )
        return run_id

    def finish_research_run(self, run_id: str, result: Mapping[str, Any] | None, error: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE research_runs SET result_json=?,status=?,error=? WHERE id=?",
                (canonical_json(dict(result)) if result is not None else None, "failed" if error else "complete", (error or "")[:1000] or None, run_id),
            )

    def open_paper_position(self, position: Mapping[str, Any]) -> str:
        position_id = str(position.get("id") or uuid.uuid4())
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO paper_positions(id,symbol,side,entry_time,entry_price,stop_price,target_price,quantity,mode,meta_json,status) VALUES(?,?,?,?,?,?,?,?,?,?, 'open')",
                (position_id, position["symbol"], position["side"], int(position["entry_time"]),
                 float(position["entry_price"]), float(position["stop_price"]), float(position["target_price"]),
                 float(position["quantity"]), str(position.get("mode", "paper")), canonical_json(dict(position.get("meta", {})))),
            )
        return position_id

    def close_paper_position(self, position_id: str, *, exit_time: int, exit_price: float, realised_pnl: float, close_reason: str) -> bool:
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE paper_positions SET status='closed',exit_time=?,exit_price=?,realised_pnl=? WHERE id=? AND status='open'",
                (int(exit_time), float(exit_price), float(realised_pnl), position_id),
            )
            if cur.rowcount == 1:
                event_payload = {"position_id": position_id, "exit_time": int(exit_time), "exit_price": float(exit_price),
                                 "realised_pnl": float(realised_pnl), "close_reason": close_reason, "simulated": True}
                encoded = canonical_json(event_payload)
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT INTO events(id,event_type,symbol,created_at,payload_json,payload_sha256,idempotency_key) "
                    "SELECT ?, 'PAPER_POSITION_CLOSED', symbol, ?, ?, ?, ? FROM paper_positions WHERE id=?",
                    (str(uuid.uuid4()), utc_now_iso(), encoded, digest, f"paper-close:{position_id}", position_id),
                )
            return cur.rowcount == 1

    def paper_positions(self, *, open_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM paper_positions" + (" WHERE status='open'" if open_only else "") + " ORDER BY entry_time"
        with self.connection() as conn:
            rows = conn.execute(query).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["meta"] = json.loads(item.pop("meta_json"))
            out.append(item)
        return out

    def research_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM research_runs ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["config"] = json.loads(item.pop("config_json"))
            item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
            out.append(item)
        return out

    def mark_outbox_unknown(self, outbox_id: str, error: str) -> None:
        """Mark ambiguous send outcomes as unknown; never auto-retry an uncertain message."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE outbox SET attempts=attempts+1, status='unknown', last_error=? WHERE id=?",
                (error[:500], outbox_id),
            )
