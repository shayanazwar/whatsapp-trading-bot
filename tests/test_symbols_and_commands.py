import pytest

from app.commands import CommandError, parse_command
from app.symbols import InvalidSymbol, normalize_symbol


def test_symbol_normalization_aliases():
    assert normalize_symbol("BTCUSDT") == "BTC_USDT"
    assert normalize_symbol("btc_usdt") == "BTC_USDT"
    assert normalize_symbol("BTC") == "BTC_USDT"
    assert normalize_symbol("ETHUSDC") == "ETH_USDC"


@pytest.mark.parametrize("bad", ["", "BTC/USDT", "BTC USDT", "BTC_USDT_PERP", "BTC!USDT", "_USDT", "BTC_EUR"])
def test_symbol_rejects_bad_input(bad):
    with pytest.raises(InvalidSymbol):
        normalize_symbol(bad)


@pytest.mark.parametrize("timeframe", ["1H", "4H", "12H", "1D"])
def test_command_parser_chart_supports_only_approved_higher_timeframes(timeframe):
    cmd = parse_command(f"CHART btc {timeframe.lower()}")
    assert cmd.name == "CHART"
    assert cmd.args == {"symbol": "BTC_USDT", "timeframe": timeframe}


@pytest.mark.parametrize("timeframe", ["5M", "15M", "30M", "5m"])
def test_command_parser_rejects_unapproved_chart_timeframes(timeframe):
    with pytest.raises(CommandError, match="1H, 4H, 12H or 1D"):
        parse_command(f"CHART BTC {timeframe}")


def test_command_parser_backtest_modes_and_windows():
    assert parse_command("BACKTEST").args["windows"] == ["1D", "3D", "7D", "30D", "90D"]
    assert parse_command("BACKTEST BTCUSDT 7D").args == {"mode": "fixed_payout", "windows": ["7D"], "symbol": "BTC_USDT"}
    cmd = parse_command("BACKTEST REALISTIC BTCUSDT 30D")
    assert cmd.args["mode"] == "exchange_realistic"
    assert cmd.args["windows"] == ["30D"]
    with pytest.raises(CommandError):
        parse_command("BACKTEST 5M")


def test_command_parser_alert_grammar():
    assert parse_command("ALERT BTCUSDT ABOVE 70000").args["direction"] == "ABOVE"
    change = parse_command("ALERT BTCUSDT CHANGE 3% 1H")
    assert change.args["threshold_pct"] == 3
    assert change.args["interval"] == "1H"
    rsi = parse_command("ALERT BTCUSDT RSI ABOVE 70 4H")
    assert rsi.args["kind"] == "rsi_cross"
    assert parse_command("ALERT DELETE abc123").args["alert_id"] == "ABC123"
    with pytest.raises(CommandError):
        parse_command("ALERT BTCUSDT CHANGE 3% 5M")


def test_control_surface_has_no_live_enable_command():
    with pytest.raises(CommandError):
        parse_command("AUTOTRADE ON")
    with pytest.raises(CommandError):
        parse_command("LIVE ON")
    assert parse_command("AUTOTRADE OFF").args["action"] == "OFF"


def test_symbol_normalization_rejects_unicode_and_delimiters():
    for symbol in ("ＢＴＣＵＳＤＴ", "BTC_USDT;DROP", "BTC%2FUSDT", "https://api.mexc.com"):
        with pytest.raises(InvalidSymbol):
            normalize_symbol(symbol)
