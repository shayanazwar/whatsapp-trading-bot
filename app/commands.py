"""Strict, deterministic command grammar; never uses an LLM to parse commands."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.symbols import InvalidSymbol, normalize_symbol

ALLOWED_ALERT_INTERVALS = {"1H", "4H", "12H", "1D"}
BACKTEST_WINDOW_ORDER = ["1D", "7D", "30D", "60D", "90D", "180D", "365D"]
ALLOWED_BACKTEST_WINDOWS = set(BACKTEST_WINDOW_ORDER)
CHART_TIMEFRAMES = frozenset({"1H", "4H", "12H", "1D"})
WHATSAPP_SUPPORT_COMMANDS = frozenset({"HELP", "STATUS"})
TP_SCENARIOS: dict[str, float] = {
    "CONTROL": 2.0,
    "1.5R": 1.5,
    "2.0R": 2.0,
    "2.5R": 2.5,
}


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


def _parse_backtest(parts: list[str]) -> Command:
    """Parse one explicit backtest run; default is 7D control, not every period at once."""
    rest = [part.upper() for part in parts[1:]]
    mode = "exchange_realistic"
    if rest and rest[0] in {"LEGACY", "FIXED", "FIXED_PAYOUT"}:
        # Retain the old explicit fixed-payout research mode as an opt-in only.
        rest.pop(0)
        mode = "fixed_payout"
    elif rest and rest[0] in {"REALISTIC", "EXCHANGE_REALISTIC"}:
        rest.pop(0)

    scenario = "CONTROL"
    reward_risk = TP_SCENARIOS[scenario]
    if len([token for token in rest if token.startswith("TP=")]) > 1:
        raise CommandError("Use only one TP scenario: CONTROL, 1.5R, 2.0R or 2.5R")
    clean: list[str] = []
    for token in rest:
        if token.startswith("TP="):
            scenario = token[3:]
            if scenario not in TP_SCENARIOS:
                raise CommandError("TP must be CONTROL, 1.5R, 2.0R or 2.5R")
            reward_risk = TP_SCENARIOS[scenario]
        else:
            clean.append(token)

    windows = [token for token in clean if token in ALLOWED_BACKTEST_WINDOWS]
    if len(windows) > 1:
        raise CommandError("Run one backtest period at a time: 1D, 7D, 30D, 60D, 90D, 180D or 365D")
    symbols = [token for token in clean if token not in ALLOWED_BACKTEST_WINDOWS]
    if any(re.fullmatch(r"\d+[A-Z]+", token) for token in symbols):
        raise CommandError("Backtest period must be 1D, 7D, 30D, 60D, 90D, 180D or 365D")
    if len(symbols) > 1:
        raise CommandError("Use at most one symbol per backtest command")

    args: dict[str, Any] = {
        "mode": mode,
        "windows": windows or ["7D"],
        "tp_scenario": scenario,
        "reward_risk_multiple": reward_risk,
    }
    if symbols:
        args["symbol"] = _symbol(symbols[0])
    if scenario != "CONTROL" and mode != "exchange_realistic":
        raise CommandError("TP=R scenarios require the exchange-realistic backtest mode")
    return Command("BACKTEST", args)


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

    if head == "PAPER":
        if len(parts) == 1:
            return Command("PAPER", {"view": "SUMMARY"})
        if len(parts) == 2 and upper[1] in {"OPEN", "HISTORY"}:
            return Command("PAPER", {"view": upper[1]})
        raise CommandError("Use PAPER, PAPER OPEN or PAPER HISTORY")

    if head == "SEARCH" and len(parts) == 2:
        query = upper[1]
        if not re.fullmatch(r"[A-Z0-9]{2,30}", query):
            raise CommandError("Search term must contain 2–30 letters or numbers")
        return Command("SEARCH", {"query": query})

    if head == "DELETE":
        if len(parts) == 2 and upper[1] == "ALL":
            return Command("DELETE_ALL_ALERTS", {})
        if len(parts) == 2:
            return Command("ALERT_DELETE", {"alert_id": upper[1]})
        raise CommandError("Use DELETE <alert_id> or DELETE ALL")

    if head in {"SIGNALS", "OPEN_POSITIONS", "POSITIONS", "PERFORMANCE", "PNL", "RISK", "LIVE_STATUS", "PAUSE", "COST"}:
        if len(parts) != 1:
            raise CommandError(f"{head} takes no arguments")
        return Command(head, {})
    if head == "AUTOTRADE":
        if len(parts) != 2 or upper[1] not in {"STATUS", "OFF"}:
            raise CommandError("Use AUTOTRADE STATUS or AUTOTRADE OFF")
        return Command("AUTOTRADE", {"action": upper[1]})

    if head in {"PRICE", "ANALYZE", "MARKET"} and len(parts) == 2:
        return Command(head, {"symbol": _symbol(parts[1])})
    if head == "SCAN" and len(parts) == 1:
        return Command("SCAN", {})
    if head == "CHART" and len(parts) == 3:
        timeframe = upper[2]
        if timeframe not in CHART_TIMEFRAMES:
            raise CommandError("CHART timeframe must be 1H, 4H, 12H or 1D")
        return Command("CHART", {"symbol": _symbol(parts[1]), "timeframe": timeframe})
    if head == "BACKTEST":
        return _parse_backtest(parts)

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
        raise CommandError("Invalid alert syntax. Try ALERT BTCUSDT ABOVE 120000 or ALERT BTCUSDT RSI ABOVE 70 4H")
    if head == "ALERTS" and len(parts) == 1:
        return Command("ALERTS", {})
    raise CommandError("Unknown command. Send HELP for the command list.")


HELP_TEXT = """📈 Pak Trading Academy Trading Bot

💰 PRICE
PRICE BTCUSDT

📊 ANALYZE
ANALYZE BTCUSDT

📈 CHART
CHART BTCUSDT 1H
CHART BTCUSDT 4H
CHART BTCUSDT 12H
CHART BTCUSDT 1D

🔔 ALERT
ALERT BTCUSDT ABOVE 120000
ALERTS
DELETE 12
DELETE ALL

🔎 SEARCH
SEARCH PEPE

🧪 BACKTEST
BACKTEST 1D
BACKTEST 7D
BACKTEST 7D TP=CONTROL
BACKTEST 7D TP=1.5R
BACKTEST 7D TP=2.0R
BACKTEST 7D TP=2.5R
BACKTEST 30D
BACKTEST 60D
BACKTEST 90D
BACKTEST 180D
BACKTEST 365D

🧪 LIVE PAPER TRADING (FAKE MONEY ONLY)
PAPER          — Wallet/equity summary
PAPER OPEN     — Current virtual positions
PAPER HISTORY  — Recent closed virtual trades

STATUS • SIGNALS • PERFORMANCE • PNL • RISK • COST
AUTOTRADE STATUS • AUTOTRADE OFF • PAUSE

⏱ SIGNAL: 1D • 12H • 4H • 1H
CHART: 1H / 4H / 12H / 1D only

Strategy uses completed candles only. Backtests are historical simulations; fees, slippage, funding and contract limits are modeled assumptions. No live orders are submitted by this release.
"""

WHATSAPP_SUPPORT_HELP_TEXT = """📈 Pak Trading Academy Bot — Service Help

HELP — Show this menu
STATUS — Bot and service status

Research commands are not enabled on this WhatsApp channel unless the applicable policy gate has been cleared.
Dashboard: https://shayan-millionaire-bot.onrender.com/dashboard
Live orders: OFF
"""
