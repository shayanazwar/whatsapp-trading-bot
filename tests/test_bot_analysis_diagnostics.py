from __future__ import annotations

import asyncio
from types import SimpleNamespace

import app.bot as bot_module
from app.bot import Bot
from app.config import Settings


def test_analyze_command_explains_no_setup_instead_of_fabricating_n_a(monkeypatch):
    async def fake_analyze_symbol(_market, symbol):
        assert symbol == "BTCUSDT"
        return {
            "symbol": "BTC_USDT",
            "setup": "NONE",
            "regime_1d": "BULLISH",
            "bias_12h": "BULLISH (HH/HL)",
            "trend_4h": "RANGE",
            "structure_4h": "RANGE",
            "value_zone_low": 100.0,
            "value_zone_high": 105.0,
            "trigger_type": "NONE (no qualified current 1H trigger)",
            "swept_level_1h": None,
            "rvol_1h": 0.62,
            "rsi": 48.5,
            "volume": "BELOW_AVERAGE",
            "score": None,
            "diagnostic_failures": ["LONG: no current valid reclaim", "SHORT: daily permission unavailable"],
        }

    monkeypatch.setattr(bot_module, "analyze_symbol", fake_analyze_symbol)
    bot = Bot(Settings(), object(), object(), object(), object())
    output: list[str] = []

    async def fake_send_text(_target, body):
        output.append(body)

    bot._send_text = fake_send_text
    asyncio.run(bot._analyze("test-recipient", "BTCUSDT"))

    assert len(output) == 1
    body = output[0]
    assert "12H Context: BULLISH (HH/HL)" in body
    assert "4H Structure: RANGE" in body
    assert "100.00000000" not in body  # price formatting is not part of the contract
    assert "Diagnostic Score: N/A (no qualified setup)" in body
    assert "Why no signal: LONG: no current valid reclaim" in body
    assert "N/A/100" not in body
