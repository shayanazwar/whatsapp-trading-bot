"""Strict, deterministic WhatsApp command grammar; never uses an LLM to parse commands."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.symbols import InvalidSymbol, normalize_symbol

ALLOWED_ALERT_INTERVALS = {"1H", "4H", "12H", "1D"}
BACKTEST_WINDOW_ORDER = ["1D", "3D", "7D", "30D", "90D"]
ALLOWED_BACKTEST_WINDOWS = set(BACKTEST_WINDOW_ORDER)

# Only these support commands may run over WhatsApp while crypto messaging is gated.
WHATSAPP_SUPPORT_COMMANDS = frozenset({"HELP", "STATUS"})
WHATSAPP_SUPPORT_HELP_TEXT = """TRADING AI — WHATSAPP HELP
Available here: HELP, STATUS
Crypto prices, analysis, scans, signals and backtests are not available on this WhatsApp channel while its policy gate is closed.
Live order execution remains disabled.
"""


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
        if timeframe != "5M":
            raise CommandError("CHART only supports the isolated visualization timeframe 5M")
        return Command("CHART", {"symbol": _symbol(parts[1]), "timeframe": "5M"})

    if head == "BACKTEST":
        args: dict[str, Any] = {"mode": "fixed_payout", "windows": list(BACKTEST_WINDOW_ORDER)}
        rest = upper[1:]
        if rest and rest[0] in {"REALISTIC", "FIXED", "FIXED_PAYOUT"}:
            mode = rest.pop(0)
            args["mode"] = "exchange_realistic" if mode == "REALISTIC" else "fixed_payout"
        if not rest:
            return Command("BACKTEST", args)
        if len(rest) == 1:
            if rest[0] not in ALLOWED_BACKTEST_WINDOWS:
                raise CommandError("Backtest period must be 1D, 3D, 7D, 30D or 90D")
            args["windows"] = [rest[0]]
            return Command("BACKTEST", args)
        if len(rest) == 2:
            if rest[1] not in ALLOWED_BACKTEST_WINDOWS:
                raise CommandError("Backtest period must be 1D, 3D, 7D, 30D or 90D")
            args["symbol"] = _symbol(rest[0])
            args["windows"] = [rest[1]]
            return Command("BACKTEST", args)
        raise CommandError("Use BACKTEST [REALISTIC] [SYMBOL] [1D|3D|7D|30D|90D]")

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


HELP_TEXT = """COMMANDS\nHELP | STATUS | PRICE BTCUSDT | CHART BTC 5M | ANALYZE BTCUSDT\nBACKTEST [REALISTIC] [BTCUSDT] [1D|3D|7D|30D|90D]\nALERT BTCUSDT ABOVE 70000 | ALERT BTCUSDT BELOW 65000\nALERT BTCUSDT CHANGE 3% 1H | ALERT BTCUSDT RSI ABOVE 70 4H\nALERTS | ALERT DELETE <id> | ALERT PAUSE <id> | ALERT RESUME <id>\nSIGNALS | OPEN_POSITIONS | PERFORMANCE | PNL | RISK | COST | PAUSE\nAUTOTRADE STATUS | AUTOTRADE OFF | LIVE_STATUS\nStrategy timeframes only: 1D, 12H, 4H, 1H. CHART 5M is visualization only.\n"""
