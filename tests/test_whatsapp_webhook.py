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


class FakeBot:
    def __init__(self):
        self.calls = []
        self.fail_once = True

    async def handle(self, sender, text):
        self.calls.append((sender, text))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("transient send failure")


def _payload(message_id="wamid.test"):
    return {
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "123"},
                    "messages": [{
                        "id": message_id,
                        "from": "923001234567",
                        "type": "text",
                        "text": {"body": "HELP"},
                    }],
                },
            }],
        }]
    }


def test_whatsapp_message_is_retryable_after_handler_failure(monkeypatch):
    db = FakeDB()
    bot = FakeBot()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "bot", bot)
    main._whatsapp_processing_messages.clear()

    asyncio.run(main.process_webhook(_payload()))
    assert bot.calls == [("923001234567", "HELP")]
    assert db.mark_calls == []

    asyncio.run(main.process_webhook(_payload()))
    assert bot.calls == [
        ("923001234567", "HELP"),
        ("923001234567", "HELP"),
    ]
    assert db.mark_calls == ["wamid.test"]
    assert main._whatsapp_processing_messages == set()


def test_successful_whatsapp_message_is_deduplicated(monkeypatch):
    db = FakeDB()

    class SuccessBot:
        def __init__(self):
            self.calls = []

        async def handle(self, sender, text):
            self.calls.append((sender, text))

    bot = SuccessBot()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "bot", bot)
    main._whatsapp_processing_messages.clear()

    asyncio.run(main.process_webhook(_payload("wamid.success")))
    asyncio.run(main.process_webhook(_payload("wamid.success")))

    assert bot.calls == [("923001234567", "HELP")]
    assert db.mark_calls == ["wamid.success"]
