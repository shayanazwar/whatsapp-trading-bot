from __future__ import annotations

import asyncio

from app.automation.signal_manager import SignalManager
from app.automation.signal_validator import ValidatedSignal
from app.automation.risk_manager import TradePlan
from app.database import SignalRecord


class FakeDB:
    def __init__(self):
        self.record = None
        self.status = None

    def get_signal(self, key):
        return self.record

    def get_last_signal_for_symbol_side(self, symbol, side):
        return self.record

    def create_signal_if_new(self, **kwargs):
        if self.record is not None:
            return False
        self.record = SignalRecord(
            signal_key=kwargs["signal_key"], symbol=kwargs["symbol"], side=kwargs["side"],
            candle_time=kwargs["candle_time"], entry=kwargs["entry"], stop_loss=kwargs["stop_loss"],
            tp1=kwargs["tp1"], tp2=kwargs["tp2"], rr=kwargs["rr"], confluence=kwargs["confluence"],
            status="NEW", analysis_json=kwargs["analysis_json"], created_at=kwargs["created_at"],
            expires_at=kwargs["expires_at"], updated_at=kwargs["created_at"],
        )
        return True

    def update_signal_status(self, signal_key, status):
        self.status = status
        if self.record:
            self.record = self.record.__class__(**{**self.record.__dict__, "status": status})
        return True


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, body):
        self.sent.append((to, body))
        return {"messages": [{"id": "ok"}]}


def signal():
    analysis = {
        "symbol": "BNB_USDT", "setup": "LONG", "score": 85,
        "setup_bos_time": 1234567890000, "bos_15m_level": 770.0,
        "rsi": 54.0, "rvol_15m": 1.2, "trend_4h": "BULLISH",
        "structure_1h": "HH/HL", "target_timeframe": "1H",
    }
    return ValidatedSignal(
        key="same-key", symbol="BNB_USDT", side="LONG", candle_time=1234567890000,
        analysis=analysis, plan=TradePlan("LONG", 772.0, 765.0, 790.0, 2.5),
    )


def test_send_failed_signal_is_retryable():
    db = FakeDB()
    wa = FakeWhatsApp()
    manager = SignalManager(db, wa, {"923001234567"}, 60)
    s = signal()

    # Seed a prior failed publication with the same structural key.
    db.record = SignalRecord(
        signal_key=s.key, symbol=s.symbol, side=s.side, candle_time=s.candle_time,
        entry=s.plan.entry, stop_loss=s.plan.stop_loss, tp1=s.plan.tp, tp2=s.plan.tp, rr=s.plan.rr,
        confluence=85, status="SEND_FAILED", analysis_json='{}', created_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-01-01T01:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
    )

    assert asyncio.run(manager.publish(s)) is True
    assert len(wa.sent) == 1
    assert db.status == "SENT"
