from __future__ import annotations

import asyncio

import app.main as main


class FakeDB:
    def __init__(self):
        self.seen = set()
        self.mark_calls = []
    def is_message_seen(self, message_id):
        return message_id in self.seen
    def mark_message_seen(self, message_id):
        self.mark_calls.append(message_id)
        if message_id in self.seen:
            return False
        self.seen.add(message_id)
        return True


def payload(message_id="wamid.success"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": "123"}, "messages": [{"id": message_id, "from": "923001234567", "type": "text", "text": {"body": "HELP"}}]}}]}]}


def test_webhook_retry_after_handler_failure(monkeypatch):
    class Bot:
        def __init__(self): self.calls = []
        async def handle(self, sender, text):
            self.calls.append((sender, text))
            if len(self.calls) == 1: raise RuntimeError("transient")
    db = FakeDB(); bot = Bot()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "bot", bot)
    main._processing_message_ids.clear()
    asyncio.run(main.process_webhook(payload("wamid.retry")))
    assert db.mark_calls == []
    asyncio.run(main.process_webhook(payload("wamid.retry")))
    assert db.mark_calls == ["wamid.retry"]
    assert len(bot.calls) == 2


def test_webhook_deduplicates_successful_message(monkeypatch):
    class Bot:
        def __init__(self): self.calls = []
        async def handle(self, sender, text): self.calls.append((sender, text))
    db = FakeDB(); bot = Bot()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "bot", bot)
    main._processing_message_ids.clear()
    asyncio.run(main.process_webhook(payload("wamid.ok")))
    asyncio.run(main.process_webhook(payload("wamid.ok")))
    assert bot.calls == [("923001234567", "HELP")]
    assert db.mark_calls == ["wamid.ok"]
