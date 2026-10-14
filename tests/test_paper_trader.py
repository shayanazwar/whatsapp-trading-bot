from __future__ import annotations

import asyncio

import pytest

from app.automation.paper_trader import PaperTrader
from app.automation.risk_manager import TradePlan
from app.automation.signal_validator import ValidatedSignal
from app.config import Settings
from app.database import Database


class FakeMexc:
    def __init__(self, price: float = 100.0) -> None:
        self.price = price

    async def get_ticker(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "lastPrice": str(self.price)}


def make_signal(key: str = "sig-1", side: str = "LONG") -> ValidatedSignal:
    if side == "LONG":
        plan = TradePlan(side="LONG", entry=100.0, stop_loss=95.0, tp=110.0, rr=2.0)
    else:
        plan = TradePlan(side="SHORT", entry=100.0, stop_loss=105.0, tp=90.0, rr=2.0)
    return ValidatedSignal(
        key=key,
        symbol="BTC_USDT",
        side=side,
        candle_time=1_800_000_000_000,
        analysis={},
        plan=plan,
    )


def make_trader(tmp_path, *, price: float = 100.0):
    db = Database(str(tmp_path / "paper.sqlite3"))
    settings = Settings(
        paper_trading_enabled=True,
        paper_initial_balance=100.0,
        paper_margin_percent=1.0,
        paper_leverage=20,
        paper_max_open_trades=3,
        paper_poll_seconds=5,
        paper_fee_rate=0.0006,
        paper_slippage_bps=2.0,
    )
    client = FakeMexc(price)
    trader = PaperTrader(db, client, settings)
    return db, client, trader


def test_paper_trade_uses_100_balance_20x_and_one_percent_margin(tmp_path):
    db, _, trader = make_trader(tmp_path)
    trade = trader.open_from_signal(make_signal())
    assert trade is not None
    assert trade["margin"] == pytest.approx(1.0)
    assert trade["leverage"] == 20
    assert trade["notional"] == pytest.approx(20.0)
    assert trade["status"] == "OPEN"
    assert db.get_paper_wallet()["initial_balance"] == pytest.approx(100.0)


def test_paper_trade_is_idempotent_for_duplicate_signal(tmp_path):
    db, _, trader = make_trader(tmp_path)
    signal = make_signal("duplicate-signal")
    assert trader.open_from_signal(signal) is not None
    assert trader.open_from_signal(signal) is None
    assert len(db.list_paper_trades(status="OPEN")) == 1


def test_long_position_closes_at_target_and_persists_net_pnl(tmp_path):
    db, client, trader = make_trader(tmp_path, price=111.0)
    trader.open_from_signal(make_signal("long-target"))
    asyncio.run(trader.monitor_once())
    open_trades = db.list_paper_trades(status="OPEN")
    closed = db.list_paper_trades(status="CLOSED")
    assert not open_trades
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "TP"
    assert closed[0]["net_pnl"] > 0
    assert db.get_paper_wallet()["balance"] > 100.0


def test_short_position_closes_at_stop_loss(tmp_path):
    db, client, trader = make_trader(tmp_path, price=106.0)
    trader.open_from_signal(make_signal("short-stop", "SHORT"))
    asyncio.run(trader.monitor_once())
    closed = db.list_paper_trades(status="CLOSED")
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "SL"
    assert closed[0]["net_pnl"] < 0


def test_unvalidated_geometry_is_not_paper_opened(tmp_path):
    db, _, trader = make_trader(tmp_path)
    bad = ValidatedSignal(
        key="bad-geometry",
        symbol="BTC_USDT",
        side="LONG",
        candle_time=1_800_000_000_000,
        analysis={},
        plan=TradePlan(side="LONG", entry=100.0, stop_loss=101.0, tp=110.0, rr=2.0),
    )
    assert trader.open_from_signal(bad) is None
    assert not db.list_paper_trades(status="OPEN")


def test_paper_mode_defaults_to_disabled_and_live_execution_is_off():
    settings = Settings()
    assert settings.paper_trading_enabled is False
    assert settings.allow_live_execution is False
    assert Settings(paper_trading_enabled=True).paper_trading_enabled is True
