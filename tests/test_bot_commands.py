from app.bot import TIMEFRAME_ALIASES, fmt_price


def test_timeframes():
    assert TIMEFRAME_ALIASES["1H"] == "1h"
    assert TIMEFRAME_ALIASES["4H"] == "4h"
    assert TIMEFRAME_ALIASES["12H"] == "12h"
    assert TIMEFRAME_ALIASES["1D"] == "1d"
    assert "5M" not in TIMEFRAME_ALIASES
    assert "15M" not in TIMEFRAME_ALIASES
    assert "30M" not in TIMEFRAME_ALIASES


def test_price_format():
    assert fmt_price(1234.56) == "1,234.56"
    assert fmt_price(1.2300) == "1.23"
    assert fmt_price(0.0001234000) == "0.0001234"
