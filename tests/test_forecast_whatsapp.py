import asyncio

import numpy as np
import pytest

from app.forecasting.kronos import _validate_and_repair, KronosForecastEngine, KronosNotReady
from app.notifications.whatsapp import (
    WhatsAppAPIError,
    WhatsAppConfigurationError,
    WhatsAppSignalAdapter,
)


def test_forecast_validation_and_repair():
    paths = np.array([[[100, 99, 101, 102, -1, 1]]], dtype=np.float32)
    fixed, counts = _validate_and_repair(paths)
    assert fixed[0, 0, 1] >= 102
    assert fixed[0, 0, 2] <= 100
    assert fixed[0, 0, 4] == 0
    assert counts["high_low_repairs"] == 2


def test_forecast_rejects_nonfinite():
    with pytest.raises(ValueError, match="NaN"):
        _validate_and_repair(np.array([[[1, 1, 1, np.nan, 1, 1]]]))


def test_forecast_rejects_nonpositive_price():
    with pytest.raises(ValueError, match="non-positive"):
        _validate_and_repair(np.array([[[0, 1, 0, 1, 1, 1]]]))


def test_model_missing_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr("app.forecasting.kronos._repo_path", lambda: tmp_path)
    engine = KronosForecastEngine()
    with pytest.raises(KronosNotReady, match="source is missing"):
        engine.load()


class FakeResponse:
    def __init__(self, status_code=200, data=None, text="", headers=None):
        self.status_code = status_code
        self._data = data or {"messages": [{"id": "wamid.test_message"}]}
        self.text = text or str(self._data)
        self.headers = headers or {}

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, json, headers):
        self.calls.append({"url": url, "json": json, "headers": headers})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_adapter(client, **extra_env):
    env = {
        "WHATSAPP_ACCESS_TOKEN": "test-access-token",
        "WHATSAPP_PHONE_NUMBER_ID": "123456789012345",
        "WHATSAPP_API_URL": "https://graph.facebook.com/v26.0",
        "WHATSAPP_DEFAULT_RECIPIENT": "+92 300 1234567",
        "WHATSAPP_MAX_ATTEMPTS": "3",
        **extra_env,
    }
    return WhatsAppSignalAdapter(environ=env, client_factory=lambda **_: client)


def test_whatsapp_is_not_falsely_enabled_without_credentials():
    adapter = WhatsAppSignalAdapter(environ={})
    assert adapter.is_enabled is False
    assert adapter.status().policy == "NOT_CONFIGURED"
    with pytest.raises(WhatsAppConfigurationError):
        asyncio.run(adapter.send_crypto_signal("Test message", "+923001234567"))


def test_whatsapp_can_be_disabled_by_environment():
    adapter = WhatsAppSignalAdapter(environ={
        "WHATSAPP_ACCESS_TOKEN": "token",
        "WHATSAPP_PHONE_NUMBER_ID": "123456789",
        "WHATSAPP_ENABLED": "false",
    })
    assert adapter.is_enabled is False
    assert adapter.status().policy == "DISABLED_BY_CONFIGURATION"


def test_whatsapp_sends_signal_to_cloud_api():
    client = FakeClient([FakeResponse()])
    adapter = make_adapter(client)

    result = asyncio.run(adapter.send_crypto_signal({
        "symbol": "BTC_USDT", "side": "LONG", "entry": 65000,
        "stop": 64000, "tp1": 67000, "tp2": 69000,
    }))

    assert result["messages"][0]["id"] == "wamid.test_message"
    call = client.calls[0]
    assert call["url"] == "https://graph.facebook.com/v26.0/123456789012345/messages"
    assert call["headers"]["Authorization"] == "Bearer test-access-token"
    assert call["json"]["messaging_product"] == "whatsapp"
    assert call["json"]["to"] == "923001234567"
    assert "BTC_USDT" in call["json"]["text"]["body"]
    assert "LONG" in call["json"]["text"]["body"]


def test_whatsapp_retries_rate_limit_then_succeeds(monkeypatch):
    client = FakeClient([
        FakeResponse(status_code=429, text="rate limited", headers={"Retry-After": "0"}),
        FakeResponse(),
    ])
    adapter = make_adapter(client)
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.notifications.whatsapp.asyncio.sleep", fake_sleep)
    result = asyncio.run(adapter.send_crypto_signal("Test signal"))

    assert result["messages"]
    assert len(client.calls) == 2
    assert delays == [0.0]


def test_whatsapp_does_not_retry_permanent_api_error():
    client = FakeClient([FakeResponse(status_code=400, text="invalid recipient")])
    adapter = make_adapter(client)

    with pytest.raises(WhatsAppAPIError) as exc:
        asyncio.run(adapter.send_crypto_signal("Test signal"))

    assert exc.value.status_code == 400
    assert len(client.calls) == 1


def test_whatsapp_rejects_invalid_recipient_before_network_call():
    client = FakeClient([FakeResponse()])
    adapter = make_adapter(client)

    with pytest.raises(ValueError, match="international number"):
        asyncio.run(adapter.send_crypto_signal("Test signal", "not-a-phone"))

    assert client.calls == []


def test_whatsapp_rejects_empty_or_oversized_messages():
    client = FakeClient([FakeResponse()])
    adapter = make_adapter(client)

    with pytest.raises(ValueError, match="cannot be empty"):
        asyncio.run(adapter.send_crypto_signal("   "))
    with pytest.raises(ValueError, match="maximum"):
        asyncio.run(adapter.send_crypto_signal("x" * 4097))
    assert client.calls == []


def test_whatsapp_accepts_meta_environment_variable_names():
    client = FakeClient([FakeResponse()])
    adapter = WhatsAppSignalAdapter(
        environ={
            "META_ACCESS_TOKEN": "meta-token",
            "META_PHONE_NUMBER_ID": "987654321",
            "META_DEFAULT_RECIPIENT": "923001234567",
        },
        client_factory=lambda **_: client,
    )

    result = asyncio.run(adapter.send_crypto_signal("Meta env test"))

    assert result["messages"]
    assert client.calls[0]["headers"]["Authorization"] == "Bearer meta-token"


def test_whatsapp_rejects_api_url_without_https():
    adapter = WhatsAppSignalAdapter(environ={
        "WHATSAPP_ACCESS_TOKEN": "token",
        "WHATSAPP_PHONE_NUMBER_ID": "123456789",
        "WHATSAPP_DEFAULT_RECIPIENT": "923001234567",
        "WHATSAPP_API_URL": "http://example.invalid/v1",
    })
    with pytest.raises(WhatsAppConfigurationError, match="HTTPS"):
        asyncio.run(adapter.send_crypto_signal("Test"))
