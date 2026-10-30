"""Dynamic, liquidity-ranked MEXC USDT perpetual universe."""
from __future__ import annotations

import math
import asyncio
from typing import Any

from app.mexc.client import MexcClient
from app.symbols import normalize_symbol


def _finite_positive(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            value = float(row.get(key))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return 0.0


async def top_usdt_perpetual_symbols(mexc: MexcClient, limit: int = 100) -> list[dict[str, Any]]:
    """Return active USDT-quoted contracts ranked by 24h turnover, not hard-coded symbols.

    Contracts without a matching ticker, valid price, or positive turnover are excluded.
    No synthetic liquidity or fallback ranking is used.
    """
    if not 1 <= int(limit) <= 500:
        raise ValueError("Universe limit must be between 1 and 500")
    contracts, tickers = await asyncio.gather(
        mexc.contract_details(), mexc.all_tickers()
    )
    # Some API deployments return a single ticker object when the optional symbol
    # parameter is omitted. Fall back to explicit public ticker requests in that case.
    if len(tickers) <= 1 and len(contracts) > 1:
        eligible_contracts = []
        for row in contracts:
            quote = str(row.get("quoteCoin") or row.get("quote_coin") or "").upper()
            try:
                active = int(row.get("state")) == 0
                perpetual = int(row.get("futureType")) == 1
            except (TypeError, ValueError):
                active, perpetual = False, False
            if quote == "USDT" and active and perpetual:
                raw = row.get("symbol") or row.get("contractCode")
                if raw:
                    eligible_contracts.append(str(raw))
        semaphore = asyncio.Semaphore(8)

        async def fetch_ticker(symbol: str):
            async with semaphore:
                try:
                    return await mexc.ticker(symbol)
                except Exception:
                    return None

        fetched = await asyncio.gather(*(fetch_ticker(symbol) for symbol in eligible_contracts))
        tickers = [row for row in fetched if isinstance(row, dict)]
    ticker_by_symbol: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        raw = ticker.get("symbol")
        if not raw:
            continue
        try:
            ticker_by_symbol[normalize_symbol(str(raw))] = ticker
        except ValueError:
            continue

    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for contract in contracts:
        raw_symbol = contract.get("symbol") or contract.get("contractCode")
        if not raw_symbol:
            continue
        try:
            symbol = normalize_symbol(str(raw_symbol))
        except ValueError:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        quote = str(contract.get("quoteCoin") or contract.get("quote_coin") or "").upper()
        if quote != "USDT":
            continue
        try:
            active = int(contract.get("state")) == 0
        except (TypeError, ValueError):
            active = False
        if not active:
            continue
        # Do not guess the contract type: only explicit MEXC perpetual metadata qualifies.
        try:
            if int(contract.get("futureType")) != 1:  # 1 = perpetual; 2 = delivery
                continue
        except (TypeError, ValueError):
            continue
        ticker = ticker_by_symbol.get(symbol)
        if not ticker:
            continue
        price = _finite_positive(ticker, "lastPrice", "last_price")
        turnover = _finite_positive(ticker, "amount24", "turnover24", "quoteVolume24", "amount")
        if price <= 0 or turnover <= 0:
            continue
        ranked.append({
            "symbol": symbol,
            "turnover_24h": turnover,
            "last_price": price,
            "contract": dict(contract),
            "ticker": dict(ticker),
        })
    ranked.sort(key=lambda item: (-item["turnover_24h"], item["symbol"]))
    return ranked[:int(limit)]
