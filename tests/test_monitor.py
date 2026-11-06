from __future__ import annotations

from dataclasses import replace

import app.monitor as monitor_module
from app.database import Database
from app.models import Candle, Signal
from app.monitor import MarketMonitor


class FakeMexc:
    def __init__(self):
        self.step = 0

    async def get_recent_closed_candles(self, count, now):
        base = 1_700_000_000 + self.step * 60
        self.step += 1
        return [Candle(base + i * 60, 100, 100.1, 99.9, 100, 1) for i in range(count)]

    async def get_ticker(self):
        import time
        return 100.0, int(time.time())


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    async def send_chunks(self, recipient, text):
        self.sent.append((recipient, text))


def test_monitor_bootstrap_skips_stale_and_only_alerts_on_fresh_edge(settings, monkeypatch):
    config = replace(settings, wa_recipients=("15550000000",))
    db = Database(config.database_path)
    mexc = FakeMexc()
    wa = FakeWhatsApp()
    sig = Signal("LONG", 1_700_000_000, 100.0, 70.0, 100.5, 0.5, 9, 65, 35)
    states = iter([sig, sig, None, sig])
    monkeypatch.setattr(monitor_module, "evaluate", lambda *_args, **_kwargs: next(states))
    monitor = MarketMonitor(config, db, mexc, wa)

    import asyncio
    async def run():
        await monitor.cycle()  # startup bootstrap — do not send
        assert not wa.sent
        await monitor.cycle()  # same condition — duplicate suppressed
        assert not wa.sent
        await monitor.cycle()  # false edge arms a future signal
        await monitor.cycle()  # fresh LONG edge — send once
    asyncio.run(run())
    assert len(wa.sent) == 1
    assert "ALERT ONLY" in wa.sent[0][1]
    assert "SL: NONE" in wa.sent[0][1]
