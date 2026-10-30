"""Strict, deterministic command grammar; never uses an LLM to parse commands."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.symbols import InvalidSymbol, normalize_symbol

ALLOWED_ALERT_INTERVALS = {"1H", "4H", "12H", "1D"}
BACKTEST_WINDOW_ORDER = ["1D", "7D", "30D", "60D", "90D"]
ALLOWED_BACKTEST_WINDOWS = set(BACKTEST_WINDOW_ORDER)
CHART_TIMEFRAMES = frozenset({"3M", "5M", "15M", "30M", "1H", "4H", "8H", "12H", "1D", "3D", "1W", "1MO"})
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
    """Parse a 3-minute RSI/EMA backtest over the current top-turnover universe."""
    rest = [part.upper() for part in parts[1:]]
    if any(token in {"LEGACY", "FIXED", "FIXED_PAYOUT", "REALISTIC", "EXCHANGE_REALISTIC"} for token in rest):
        raise CommandError("This release uses the 3M RSI/EMA backtest only; legacy modes are unavailable.")
    if any(token.startswith("TP=") for token in rest):
        raise CommandError("3M RSI/EMA uses configured net TP/SL values; TP scenario overrides are not supported.")
    clean = list(rest)
    windows = [token for token in clean if token in ALLOWED_BACKTEST_WINDOWS]
    if len(windows) > 1:
        raise CommandError("Run one backtest period at a time: 1D, 7D, 30D, 60D or 90D")
    symbols = [token for token in clean if token not in ALLOWED_BACKTEST_WINDOWS]
    if any(re.fullmatch(r"\d+[A-Z]+", token) for token in symbols):
        raise CommandError("Backtest period must be 1D, 7D, 30D, 60D or 90D")
    if len(symbols) > 1:
        raise CommandError("Use at most one symbol per backtest command")
    if not symbols and windows and windows[0] not in {"1D", "7D"}:
        raise CommandError(
            "Top-100 3M backtests support 1D or 7D only. Add one symbol for longer periods, e.g. BACKTEST BTCUSDT 30D."
        )
    args: dict[str, Any] = {"mode": "rsi_ema", "windows": windows or ["7D"]}
    if symbols:
        args["symbol"] = _symbol(symbols[0])
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
            raise CommandError("CHART timeframe must be a supported MEXC interval or derived 3M/12H/3D interval")
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
CHART BTCUSDT 3M
CHART BTCUSDT 1H
CHART BTCUSDT 4H
CHART BTCUSDT 12H
CHART BTCUSDT 1D

🔎 MARKET SCAN
SCAN — Top 100 active MEXC USDT perpetuals by 24H turnover
SIGNALS — Latest 3M scan results
SEARCH PEPE

🧪 BACKTEST — 3M RSI/EMA
BACKTEST 1D
BACKTEST 7D
BACKTEST BTCUSDT 30D — Single-symbol longer test
BACKTEST BTCUSDT 60D
BACKTEST BTCUSDT 90D

🧪 PAPER (FAKE MONEY ONLY)
PAPER
PAPER OPEN
PAPER HISTORY

🔔 ALERTS
ALERT BTCUSDT ABOVE 120000
ALERTS
DELETE 12
DELETE ALL

STATUS • PERFORMANCE • PNL • RISK • COST
AUTOTRADE STATUS • AUTOTRADE OFF • PAUSE

Strategy: completed 3-minute candles · LONG and SHORT RSI/EMA momentum · EMA 21/50/100 alignment and crossover · prior-candle breakout/breakdown · relative-volume confirmation.
BACKTEST: Cross-margin single-position research model by default, $100 starting wallet, $1 initial margin, 50x leverage cap, +$0.30 net TP, -$3.00 requested SL, 24-hour maximum holding period. Actual contract leverage and quantity limits apply.
Top universe: active USDT perpetuals ranked by 24H turnover. Live orders: OFF.
"""

WHATSAPP_SUPPORT_HELP_TEXT = """📈 Pak Trading Academy Bot — Service Help

HELP — Show this menu
STATUS — Bot and service status

Research commands are not enabled on this WhatsApp channel unless the applicable policy gate has been cleared.
Dashboard: https://shayan-millionaire-bot.onrender.com/dashboard
Live orders: OFF
"""
