from __future__ import annotations

import asyncio
import json

from app.whatsapp import WhatsAppClient, WhatsAppError


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.is_error = status_code >= 400
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_whatsapp_client_chunks_long_text():
    client = WhatsAppClient("TOKEN", "123", "v26.0", "SECRET")
    calls = []

    async def fake_post(url, **kwargs):
        calls.append(kwargs["content"])
        return FakeResponse({"messages": [{"id": str(len(calls))}]})

    client.http.post = fake_post
    result = asyncio.run(client.send_text("923001234567", "x" * 7001))
    asyncio.run(client.close())

    assert result["chunks"] == 3
    payloads = [json.loads(raw.decode("utf-8")) for raw in calls]
    assert [len(p["text"]["body"]) for p in payloads] == [3500, 3500, 1]


def test_whatsapp_client_retries_transient_errors():
    client = WhatsAppClient("TOKEN", "123", "v26.0", "SECRET")
    calls = []

    async def fake_post(url, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            return FakeResponse({"error": {"message": "busy"}}, status_code=503)
        return FakeResponse({"messages": [{"id": "ok"}]})

    client.http.post = fake_post
    original_sleep = asyncio.sleep

    async def fake_sleep(_delay):
        return None

    asyncio.sleep = fake_sleep
    try:
        result = asyncio.run(client.send_text("923001234567", "hello"))
    finally:
        asyncio.sleep = original_sleep
        asyncio.run(client.close())

    assert result["messages"][0]["id"] == "ok"
    assert len(calls) == 3


def test_whatsapp_client_raises_non_retryable_error():
    client = WhatsAppClient("TOKEN", "123", "v26.0", "SECRET")

    async def fake_post(url, **kwargs):
        return FakeResponse({"error": {"message": "bad request"}}, status_code=400)

    client.http.post = fake_post
    try:
        try:
            asyncio.run(client.send_text("923001234567", "hello"))
        except WhatsAppError as exc:
            assert "400" in str(exc)
        else:
            raise AssertionError("Expected WhatsAppError")
    finally:
        asyncio.run(client.close())
