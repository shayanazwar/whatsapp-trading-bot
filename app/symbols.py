"""Strict symbol normalization for user commands and MEXC Futures contracts."""
from __future__ import annotations

import re

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,24}(?:_[A-Z0-9]{2,12})?$")
_VALID_QUOTES = {"USDT", "USDC", "USD"}


class InvalidSymbol(ValueError):
    pass


def normalize_symbol(value: str, *, allow_bare_base: bool = True) -> str:
    """Normalize BTCUSDT/BTC_USDT/BTC to MEXC-style BTC_USDT.

    This validates syntax only. Contract existence/eligibility must be verified against
    MEXC contract metadata before analysis or any future order.
    """
    if not isinstance(value, str):
        raise InvalidSymbol("Symbol must be text")
    raw = value.strip().upper()
    if not raw or len(raw) > 36 or any(ch.isspace() for ch in raw):
        raise InvalidSymbol("Symbol is empty, too long, or contains whitespace")
    if not re.fullmatch(r"[A-Z0-9_]+", raw):
        raise InvalidSymbol("Symbol contains unsupported characters")

    if "_" in raw:
        if raw.count("_") != 1 or not _SYMBOL_RE.fullmatch(raw):
            raise InvalidSymbol("Expected symbol format BASE_QUOTE")
        base, quote = raw.split("_", 1)
        if not base or quote not in _VALID_QUOTES:
            raise InvalidSymbol("Unsupported quote asset in symbol")
        return f"{base}_{quote}"

    for quote in sorted(_VALID_QUOTES, key=len, reverse=True):
        if raw.endswith(quote) and len(raw) > len(quote):
            base = raw[: -len(quote)]
            if not base or not re.fullmatch(r"[A-Z0-9]+", base):
                raise InvalidSymbol("Invalid base asset")
            return f"{base}_{quote}"

    if allow_bare_base and re.fullmatch(r"[A-Z0-9]{1,20}", raw):
        return f"{raw}_USDT"
    raise InvalidSymbol("Use a MEXC Futures symbol such as BTCUSDT or BTC_USDT")


def is_symbol_syntax_valid(value: str) -> bool:
    try:
        normalize_symbol(value)
        return True
    except InvalidSymbol:
        return False
