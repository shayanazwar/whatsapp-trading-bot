"""Strict, deterministic WhatsApp command grammar; never uses an LLM to parse commands."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.symbols import InvalidSymbol, normalize_symbol

ALLOWED_ALERT_INTERVALS = {"1H", "4H", "12H", "1D"}
BACKTEST_WINDOW_ORDER = ["1D", "7D", "30D", "90D"]
ALLOWED_BACKTEST_WINDOWS = set(BACKTEST_WINDOW_ORDER)
CHART_TIMEFRAMES = frozenset({"5M", "15M", "1H", "4H", "8H", "12H", "1D", "3D", "1W", "1MO"})
WHATSAPP_SUPPORT_COMMANDS = frozenset({"HELP", "STATUS"})


class CommandError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    args: dict[str, Any]


def _symbol(value: str) -> str:
    try:
        return normalize_symbol(value)
    except InvalidSymbol as exc:
        raise CommandError(str(exc)) from exc


def parse_command(text: str) -> Command:
    if not isinstance(text, str):
        raise CommandError("Command must be text")
    raw = text.strip()
    if not raw or len(raw) > 500:
        raise CommandError("Command is empty or too long")
    parts = re.split(r"\s+", raw)
    upper = [p.upper() for p in parts]
    head = upper[0]

    if head in {"HELP", "/HELP"} and len(parts) == 1:
        return Command("HELP", {})
    if head in {"STATUS", "/STATUS"} and len(parts) == 1:
        return Command("STATUS", {})
    if head in {"SIGNALS", "OPEN_POSITIONS", "POSITIONS", "PERFORMANCE", "PNL", "RISK", "LIVE_STATUS", "AUTOTRADE", "PAUSE", "COST"}:
        if head in {"PAUSE", "COST"}:
            if len(parts) != 1:
                raise CommandError(f"{head} takes no arguments")
            return Command(head, {})
        if head == "AUTOTRADE":
            if len(parts) != 2 or upper[1] not in {"STATUS", "OFF"}:
                raise CommandError("Use AUTOTRADE STATUS or AUTOTRADE OFF")
            return Command("AUTOTRADE", {"action": upper[1]})
        if len(parts) != 1:
            raise CommandError(f"{head} takes no arguments")
        return Command(head, {})

    if head == "PRICE" and len(parts) == 2:
        return Command("PRICE", {"symbol": _symbol(parts[1])})
    if head == "ANALYZE" and len(parts) == 2:
        return Command("ANALYZE", {"symbol": _symbol(parts[1])})
    if head == "MARKET" and len(parts) == 2:
        return Command("MARKET", {"symbol": _symbol(parts[1])})
    if head == "SCAN" and len(parts) == 1:
        return Command("SCAN", {})
    if head == "CHART" and len(parts) == 3:
        timeframe = upper[2]
        if timeframe not in CHART_TIMEFRAMES:
            raise CommandError("CHART timeframe must be 5M, 15M, 1H, 4H, 8H, 12H, 1D, 3D, 1W or 1MO")
        return Command("CHART", {"symbol": _symbol(parts[1]), "timeframe": timeframe})

    if head == "BACKTEST":
        # Selected RSI/EMA strategy is the default. The old research model remains explicit.
        args: dict[str, Any] = {"mode": "rsi_ema", "windows": list(BACKTEST_WINDOW_ORDER)}
        rest = upper[1:]
        if rest and rest[0] in {"LEGACY", "REALISTIC", "FIXED", "FIXED_PAYOUT"}:
            mode = rest.pop(0)
            args["mode"] = "fixed_payout" if mode in {"FIXED", "FIXED_PAYOUT"} else "exchange_realistic"
        if not rest:
            return Command("BACKTEST", args)
        if len(rest) == 1:
            if rest[0] in ALLOWED_BACKTEST_WINDOWS:
                args["windows"] = [rest[0]]
            elif re.fullmatch(r"\d+[A-Z]+", rest[0]):
                raise CommandError("Backtest period must be 1D, 7D, 30D or 90D")
            else:
                args["symbol"] = _symbol(rest[0])
            return Command("BACKTEST", args)
        if len(rest) == 2:
            if rest[0] in ALLOWED_BACKTEST_WINDOWS:
                args["windows"] = [rest[0]]
                args["symbol"] = _symbol(rest[1])
            else:
                args["symbol"] = _symbol(rest[0])
                if rest[1] not in ALLOWED_BACKTEST_WINDOWS:
                    raise CommandError("Backtest period must be 1D, 7D, 30D or 90D")
                args["windows"] = [rest[1]]
            return Command("BACKTEST", args)
        raise CommandError("Use BACKTEST [SYMBOL] [1D|7D|30D|90D] or BACKTEST LEGACY [SYMBOL] [period]")

    if head == "ALERT" and len(parts) >= 3:
        if upper[1] == "DELETE" and len(parts) == 3:
            return Command("ALERT_DELETE", {"alert_id": parts[2].upper()})
        if upper[1] == "PAUSE" and len(parts) == 3:
            return Command("ALERT_PAUSE", {"alert_id": parts[2].upper()})
        if upper[1] == "RESUME" and len(parts) == 3:
            return Command("ALERT_RESUME", {"alert_id": parts[2].upper()})
        symbol = _symbol(parts[1])
        if upper[2] in {"ABOVE", "BELOW"} and len(parts) == 4:
            try:
                level = float(parts[3])
            except ValueError as exc:
                raise CommandError("Price alert level must be numeric") from exc
            if level <= 0:
                raise CommandError("Price alert level must be positive")
            return Command("ALERT_CREATE", {"symbol": symbol, "kind": "price_cross", "direction": upper[2], "level": level})
        if upper[2] == "CHANGE" and len(parts) == 5:
            match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)%", parts[3])
            if not match:
                raise CommandError("Change threshold must look like 3%")
            threshold = abs(float(match.group(1)))
            interval = upper[4]
            if threshold <= 0 or interval not in ALLOWED_ALERT_INTERVALS:
                raise CommandError("Change threshold must be positive and interval must be 1H, 4H, 12H or 1D")
            return Command("ALERT_CREATE", {"symbol": symbol, "kind": "percent_change", "threshold_pct": threshold, "interval": interval})
        if upper[2] == "RSI" and len(parts) == 6 and upper[3] in {"ABOVE", "BELOW"}:
            try:
                threshold = float(parts[4])
            except ValueError as exc:
                raise CommandError("RSI threshold must be numeric") from exc
            interval = upper[5]
            if not 0 <= threshold <= 100 or interval not in ALLOWED_ALERT_INTERVALS:
                raise CommandError("RSI threshold must be 0..100 and interval must be 1H, 4H, 12H or 1D")
            return Command("ALERT_CREATE", {"symbol": symbol, "kind": "rsi_cross", "direction": upper[3], "threshold": threshold, "interval": interval})
        raise CommandError("Invalid alert syntax. Try ALERT BTCUSDT ABOVE 70000 or ALERT BTCUSDT RSI ABOVE 70 4H")
    if head == "ALERTS" and len(parts) == 1:
        return Command("ALERTS", {})
    raise CommandError("Unknown command. Send HELP for the command list.")


HELP_TEXT = """🤖 PAK TRADING ACADEMY — COMMAND MENU

HELP | STATUS
PRICE BTCUSDT | MARKET BTCUSDT | ANALYZE BTCUSDT
CHART BTCUSDT 5M (also 15M, 1H, 4H, 8H, 12H, 1D, 3D, 1W, 1MO)
SCAN | SIGNALS | BACKTEST 7D | BACKTEST LEGACY BTCUSDT 30D
PERFORMANCE | PNL | RISK | OPEN_POSITIONS | LIVE_STATUS
ALERT BTCUSDT ABOVE 70000 | ALERTS | ALERT DELETE <id>
AUTOTRADE STATUS | AUTOTRADE OFF | PAUSE | COST

Selected entry strategy: completed 3-minute candles; RSI above 70 rising + EMA 21/50/100 bullish crossover/alignment + breakout + volume confirmation.
BACKTEST uses the selected RSI/EMA strategy by default; BACKTEST LEGACY keeps the older research model.
No live orders are available in this release.
"""

WHATSAPP_SUPPORT_HELP_TEXT = """🤖 PAK TRADING ACADEMY — COMMAND MENU

HELP — Show this menu
STATUS — Bot and service status

RESEARCH COMMANDS
PRICE BTCUSDT — Current MEXC Futures price
ANALYZE BTCUSDT — Multi-timeframe analysis
CHART BTCUSDT 5M — Candlestick chart (15M / 1H / 4H / 8H / 12H / 1D / 3D / 1W / 1MO also supported)
SCAN — Scan configured symbols
SIGNALS — Recent recorded candidates
BACKTEST 7D — Seven-day RSI/EMA historical test
PERFORMANCE — Recent report summary
RISK — Research risk limits

Research commands on WhatsApp require the channel policy gate to be cleared.
Dashboard: https://shayan-millionaire-bot.onrender.com/dashboard
Live orders: OFF
"""
