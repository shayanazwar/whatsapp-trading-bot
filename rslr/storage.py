from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from rslr.config import settings


def _database_url(value: str) -> str:
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+psycopg://", 1)
    if value.startswith("postgresql://") and "+psycopg" not in value:
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


class Base(DeclarativeBase):
    pass


class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    side: Mapped[str] = mapped_column(String(8))
    module: Mapped[str] = mapped_column(String(30))
    signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class BacktestRow(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[str] = mapped_column(String(8), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    payload: Mapped[str] = mapped_column(Text)


class NotificationRow(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    recipient: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    payload: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    external_message_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OISnapshotRow(Base):
    __tablename__ = "open_interest_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    hold_volume: Mapped[float] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


class JobLockRow(Base):
    __tablename__ = "job_locks"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    owner: Mapped[str] = mapped_column(String(100))


class ContractRow(Base):
    __tablename__ = "contracts"
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class CandleRow(Base):
    __tablename__ = "candles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    interval: Mapped[str] = mapped_column(String(4))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("symbol", "interval", "timestamp", name="uq_candle_symbol_interval_time"),)


class FundingRow(Base):
    __tablename__ = "funding_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    funding_rate: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("symbol", "timestamp", name="uq_funding_symbol_time"),)


class UniverseSnapshotRow(Base):
    __tablename__ = "market_universe_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    symbols: Mapped[str] = mapped_column(Text)


class BacktestTradeRow(Base):
    __tablename__ = "backtest_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    result: Mapped[str] = mapped_column(String(16))
    net_pnl: Mapped[float] = mapped_column(Float)
    payload: Mapped[str] = mapped_column(Text)


class PaperPositionRow(Base):
    __tablename__ = "paper_positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_fingerprint: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    side: Mapped[str] = mapped_column(String(8))
    module: Mapped[str] = mapped_column(String(30))
    entry: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    contracts: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[str] = mapped_column(String(16), default="OPEN")


class SignalOutcomeRow(Base):
    __tablename__ = "signal_outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_fingerprint: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    result: Mapped[str] = mapped_column(String(16))
    pnl_reference: Mapped[float] = mapped_column(Float, default=0.0)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExecutionLogRow(Base):
    __tablename__ = "execution_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SystemLogRow(Base):
    __tablename__ = "system_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(12))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Database:
    def __init__(self, url: str | None = None) -> None:
        database_url = _database_url(url or settings.database_url)
        options: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite:"):
            options["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(database_url, **options)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def save_signal(self, signal: dict[str, Any]) -> bool:
        from sqlalchemy.exc import IntegrityError

        stamp = signal["signal_time"]
        fingerprint = f"{signal['symbol']}|{signal['side']}|{signal['module']}|{stamp}"
        try:
            with self.Session.begin() as session:
                exists = session.scalar(select(SignalRow.id).where(SignalRow.fingerprint == fingerprint))
                if exists:
                    return False
                session.add(
                    SignalRow(
                        fingerprint=fingerprint,
                        symbol=signal["symbol"],
                        side=signal["side"],
                        module=signal["module"],
                        signal_time=datetime.fromisoformat(stamp),
                        payload=json.dumps(signal, default=str),
                    )
                )
                if settings.paper_trading_enabled:
                    session.add(
                        PaperPositionRow(
                            signal_fingerprint=fingerprint,
                            symbol=signal["symbol"],
                            side=signal["side"],
                            module=signal["module"],
                            entry=float(signal["reference_entry"]),
                            stop_loss=float(signal["stop_loss"]),
                            take_profit=float(signal["take_profit"]),
                            contracts=float(signal["contracts"]),
                            opened_at=datetime.fromisoformat(stamp),
                        )
                    )
            return True
        except IntegrityError:
            return False

    def latest_signals(self, limit: int = 5) -> list[dict[str, Any]]:
        with self.Session() as session:
            rows = session.scalars(select(SignalRow).order_by(SignalRow.signal_time.desc()).limit(limit)).all()
            return [json.loads(row.payload) for row in rows]

    def save_backtest(self, payload: dict[str, Any]) -> int:
        with self.Session.begin() as session:
            row = BacktestRow(period=payload["period"], payload=json.dumps(payload, default=str))
            session.add(row)
            session.flush()
            for trade in payload.get("trades", []):
                session.add(BacktestTradeRow(
                    run_id=row.id, symbol=trade["symbol"], result=trade["result"],
                    net_pnl=float(trade["net_pnl"]), payload=json.dumps(trade, default=str)
                ))
            return row.id

    def latest_backtest(self) -> dict[str, Any] | None:
        with self.Session() as session:
            row = session.scalar(select(BacktestRow).order_by(BacktestRow.created_at.desc()).limit(1))
            return json.loads(row.payload) if row else None

    def enqueue_notification(self, recipient: str, dedupe_key: str, payload: dict[str, Any]) -> bool:
        with self.Session.begin() as session:
            exists = session.scalar(select(NotificationRow.id).where(NotificationRow.dedupe_key == dedupe_key))
            if exists:
                return False
            session.add(NotificationRow(recipient=recipient, dedupe_key=dedupe_key, payload=json.dumps(payload)))
        return True

    def pending_notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.Session() as session:
            rows = session.scalars(select(NotificationRow).where(NotificationRow.status == "PENDING").order_by(NotificationRow.id).limit(limit)).all()
            return [{"id": r.id, "recipient": r.recipient, "payload": json.loads(r.payload), "attempts": r.attempts} for r in rows]

    def update_notification(
        self, notification_id: int, status: str, error: str = "", external_message_id: str | None = None
    ) -> None:
        with self.Session.begin() as session:
            row = session.get(NotificationRow, notification_id)
            if row:
                row.status = status
                row.attempts += 1
                row.last_error = error[:2000]
                if external_message_id:
                    row.external_message_id = external_message_id
                if status in {"SENT", "DELIVERED", "READ"}:
                    row.sent_at = row.sent_at or datetime.now(timezone.utc)

    def update_notification_by_external_id(self, external_message_id: str, status: str, error: str = "") -> bool:
        normalized = status.upper()
        if normalized not in {"SENT", "DELIVERED", "READ", "FAILED"}:
            return False
        with self.Session.begin() as session:
            row = session.scalar(select(NotificationRow).where(
                NotificationRow.external_message_id == external_message_id
            ))
            if row is None:
                return False
            row.status = normalized
            if error:
                row.last_error = error[:2000]
            if normalized in {"SENT", "DELIVERED", "READ"}:
                row.sent_at = row.sent_at or datetime.now(timezone.utc)
            return True

    def save_oi_snapshots(self, tickers: list[dict[str, Any]]) -> None:
        with self.Session.begin() as session:
            for ticker in tickers:
                symbol = str(ticker.get("symbol") or "")
                value = ticker.get("holdVol")
                if symbol and value not in (None, ""):
                    try:
                        session.add(OISnapshotRow(symbol=symbol, hold_volume=float(value)))
                    except (TypeError, ValueError):
                        continue

    def save_market_metadata(self, contracts: list[Any], symbols: list[str]) -> None:
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            for contract in contracts:
                payload = json.dumps(contract.__dict__, default=str)
                row = session.get(ContractRow, contract.symbol)
                if row:
                    row.payload = payload
                    row.updated_at = now
                else:
                    session.add(ContractRow(symbol=contract.symbol, payload=payload, updated_at=now))
            session.add(UniverseSnapshotRow(symbols=json.dumps(symbols), captured_at=now))

    def save_candles(self, symbol: str, interval: str, frame: Any) -> None:
        with self.Session.begin() as session:
            for row in frame.itertuples(index=False):
                stamp = row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp
                exists = session.scalar(select(CandleRow.id).where(
                    CandleRow.symbol == symbol, CandleRow.interval == interval, CandleRow.timestamp == stamp
                ))
                if not exists:
                    session.add(CandleRow(
                        symbol=symbol, interval=interval, timestamp=stamp, open=float(row.open), high=float(row.high),
                        low=float(row.low), close=float(row.close), volume=float(row.volume)
                    ))

    def save_funding(self, symbol: str, frame: Any) -> None:
        with self.Session.begin() as session:
            for row in frame.itertuples(index=False):
                stamp = row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp
                exists = session.scalar(select(FundingRow.id).where(FundingRow.symbol == symbol, FundingRow.timestamp == stamp))
                if not exists:
                    session.add(FundingRow(symbol=symbol, timestamp=stamp, funding_rate=float(row.funding_rate)))

    def open_paper_positions(self) -> list[dict[str, Any]]:
        with self.Session() as session:
            rows = session.scalars(select(PaperPositionRow).where(PaperPositionRow.state == "OPEN")).all()
            result = []
            for row in rows:
                opened = row.opened_at
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                result.append({
                    "id": row.id, "symbol": row.symbol, "side": row.side, "module": row.module,
                    "entry": row.entry, "stop_loss": row.stop_loss, "take_profit": row.take_profit,
                    "contracts": row.contracts, "opened_at": opened, "signal_fingerprint": row.signal_fingerprint,
                })
            return result

    def close_paper_position(self, position_id: int, result: str, exit_price: float, closed_at: datetime, pnl_reference: float) -> None:
        with self.Session.begin() as session:
            row = session.get(PaperPositionRow, position_id)
            if not row or row.state != "OPEN":
                return
            row.state = result
            row.result = result
            row.exit_price = float(exit_price)
            row.closed_at = closed_at
            session.add(SignalOutcomeRow(
                signal_fingerprint=row.signal_fingerprint, symbol=row.symbol, result=result,
                pnl_reference=float(pnl_reference), resolved_at=closed_at
            ))

    def release_lock(self, name: str, owner: str) -> bool:
        """Release a named worker lock only when this process still owns it."""
        with self.Session.begin() as session:
            row = session.get(JobLockRow, name)
            if row is None or row.owner != owner:
                return False
            session.delete(row)
            return True

    def acquire_lock(self, name: str, owner: str, ttl_seconds: int = 300) -> bool:
        from datetime import timedelta
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        now = datetime.now(timezone.utc)
        expiry = now + timedelta(seconds=ttl_seconds)
        try:
            with self.Session.begin() as session:
                row = session.scalar(select(JobLockRow).where(JobLockRow.name == name).with_for_update())
                if row:
                    stored_expiry = row.expires_at
                    if stored_expiry.tzinfo is None:
                        stored_expiry = stored_expiry.replace(tzinfo=timezone.utc)
                    if stored_expiry > now:
                        return False
                if row:
                    row.owner = owner
                    row.expires_at = expiry
                else:
                    session.add(JobLockRow(name=name, owner=owner, expires_at=expiry))
                    session.flush()
            return True
        except IntegrityError:
            return False
