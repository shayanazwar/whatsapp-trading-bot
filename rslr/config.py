from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Deliberately immutable safety constants: no .env setting can arm live trading.
HARD_LIVE_EXECUTION_ALLOWED = False
HARD_AUTO_TRADING_ALLOWED = False


def _env_first(*names: str, default: str = "") -> str:
    """First non-empty value among env var names (primary, then legacy aliases).

    The deployed Render service carries the WhatsApp credentials under the
    META_* / WHATSAPP_ALLOWED_SENDERS names; the WHATSAPP_* names take
    precedence when both are set.
    """
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _env_list(*names: str) -> tuple[str, ...]:
    raw = _env_first(*names)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./rslr.db")
    mexc_base_url: str = os.getenv("MEXC_BASE_URL", "https://api.mexc.com").rstrip("/")
    mexc_timeout_seconds: float = _float("MEXC_TIMEOUT_SECONDS", 12.0)
    mexc_max_retries: int = _int("MEXC_MAX_RETRIES", 3)
    universe_size: int = _int("UNIVERSE_SIZE", 25)
    min_24h_quote_volume: float = _float("MIN_24H_QUOTE_VOLUME", 1_000_000.0)
    enable_sweep: bool = _bool("ENABLE_SWEEP", True)
    enable_donchian: bool = _bool("ENABLE_DONCHIAN", True)
    account_equity_usd: float = _float("ACCOUNT_EQUITY_USD", 100.0)
    risk_per_trade: float = _float("RISK_PER_TRADE", 0.005)
    taker_fee_rate: float = _float("TAKER_FEE_RATE", 0.0008)
    slippage_rate: float = _float("SLIPPAGE_RATE", 0.0005)
    default_leverage: int = _int("DEFAULT_LEVERAGE", 2)
    max_leverage: int = _int("MAX_LEVERAGE", 5)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 3)
    max_margin_fraction: float = _float("MAX_MARGIN_FRACTION", 0.25)
    max_hold_hours: int = _int("MAX_HOLD_HOURS", 72)
    max_signals_per_scan: int = _int("MAX_SIGNALS_PER_SCAN", 3)
    max_signals_per_24h: int = _int("MAX_SIGNALS_PER_24H", 8)
    same_symbol_cooldown_hours: int = _int("SAME_SYMBOL_COOLDOWN_HOURS", 4)
    scan_interval_seconds: int = _int("SCAN_INTERVAL_SECONDS", 3600)
    whatsapp_access_token: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    whatsapp_phone_number_id: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    whatsapp_verify_token: str = _env_first("WHATSAPP_VERIFY_TOKEN", "META_VERIFY_TOKEN")
    whatsapp_app_secret: str = _env_first("WHATSAPP_APP_SECRET", "META_APP_SECRET")
    whatsapp_graph_version: str = os.getenv("WHATSAPP_GRAPH_VERSION", "v26.0")
    whatsapp_allowed_numbers: tuple[str, ...] = _env_list(
        "WHATSAPP_ALLOWED_NUMBERS", "WHATSAPP_ALLOWED_SENDERS"
    )
    whatsapp_signal_template_name: str = os.getenv("WHATSAPP_SIGNAL_TEMPLATE_NAME", "")
    whatsapp_template_language: str = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en_US")
    paper_trading_enabled: bool = _bool("PAPER_TRADING_ENABLED", True)
    # These values are displayed for diagnostics only. They are not used to arm execution.
    auto_trading_config_value: bool = _bool("AUTO_TRADING_ENABLED", False)
    live_execution_config_value: bool = _bool("LIVE_EXECUTION_ENABLED", False)


settings = Settings()


def ensure_output_dirs() -> None:
    Path("charts").mkdir(exist_ok=True)
    Path("backtest_results").mkdir(exist_ok=True)
