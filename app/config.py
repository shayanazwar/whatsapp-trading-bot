"""Environment-driven settings with safe defaults and no secret logging."""
from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # .env support is optional at runtime.
    pass


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (true/false).")


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}.")
    return value


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric.") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite.")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}.")
    return value


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    log_level: str = "INFO"
    database_path: str = "data/shayan_ai.sqlite3"
    mexc_base_url: str = "https://contract.mexc.com"
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    scan_interval_seconds: int = 60
    scan_history_days: int = 10
    signal_freshness_minutes: int = 10
    enable_scanner: bool = False
    enable_whatsapp: bool = False
    enable_telegram: bool = False
    enable_telegram_commands: bool = True
    telegram_bot_token: str = ""
    telegram_chat_ids: tuple[str, ...] = ()
    meta_access_token: str = ""
    meta_phone_number_id: str = ""
    meta_app_secret: str = ""
    meta_verify_token: str = ""
    meta_graph_api_version: str = "v26.0"
    whatsapp_recipients: tuple[str, ...] = ()
    initial_capital: float = 10_000.0
    risk_per_trade_pct: float = 0.005
    backtest_fixed_risk_usd: float = 5.0
    fee_bps_per_side: float = 4.0
    slippage_bps_per_side: float = 2.0
    funding_bps_per_8h: float = 0.0
    max_concurrent_positions: int = 1
    strategy_pivot_left: int = 2
    strategy_pivot_right: int = 2
    strategy_atr_period: int = 14
    strategy_displacement_atr: float = 0.6
    strategy_max_bars_after_sweep: int = 8
    strategy_min_rr: float = 1.5
    strategy_stop_buffer_atr: float = 0.1
    strategy_signal_expiry_minutes: int = 60
    webhook_max_body_bytes: int = 1_048_576

    @classmethod
    def from_env(cls) -> "Settings":
        symbols = tuple(
            s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()
        )
        recipients = tuple(s.strip() for s in os.getenv("WHATSAPP_RECIPIENT_ALLOWLIST", "").split(",") if s.strip())
        telegram_chat_ids = tuple(s.strip() for s in os.getenv("TELEGRAM_CHAT_ID_ALLOWLIST", "").split(",") if s.strip())
        settings = cls(
            app_env=os.getenv("APP_ENV", "development").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            database_path=os.getenv("DATABASE_PATH", "data/shayan_ai.sqlite3").strip(),
            mexc_base_url=os.getenv("MEXC_BASE_URL", "https://contract.mexc.com").rstrip("/"),
            symbols=symbols,
            scan_interval_seconds=_int("SCAN_INTERVAL_SECONDS", 60),
            scan_history_days=_int("SCAN_HISTORY_DAYS", 10),
            signal_freshness_minutes=_int("SIGNAL_FRESHNESS_MINUTES", 10),
            enable_scanner=_bool("ENABLE_SCANNER", False),
            enable_whatsapp=_bool("ENABLE_WHATSAPP", False),
            enable_telegram=_bool("ENABLE_TELEGRAM", False),
            enable_telegram_commands=_bool("ENABLE_TELEGRAM_COMMANDS", True),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_ids=telegram_chat_ids,
            meta_access_token=os.getenv("META_ACCESS_TOKEN", "").strip(),
            meta_phone_number_id=os.getenv("META_PHONE_NUMBER_ID", "").strip(),
            meta_app_secret=os.getenv("META_APP_SECRET", "").strip(),
            meta_verify_token=os.getenv("META_VERIFY_TOKEN", "").strip(),
            meta_graph_api_version=os.getenv("META_GRAPH_API_VERSION", "v26.0").strip(),
            whatsapp_recipients=recipients,
            initial_capital=_float("INITIAL_CAPITAL", 10_000.0, 0.01),
            risk_per_trade_pct=_float("RISK_PER_TRADE_PCT", 0.005, 0.0001),
            backtest_fixed_risk_usd=_float("BACKTEST_FIXED_RISK_USD", 5.0, 0.01),
            fee_bps_per_side=_float("FEE_BPS_PER_SIDE", 4.0, 0.0),
            slippage_bps_per_side=_float("SLIPPAGE_BPS_PER_SIDE", 2.0, 0.0),
            funding_bps_per_8h=_float("FUNDING_BPS_PER_8H", 0.0, 0.0),
            max_concurrent_positions=_int("MAX_CONCURRENT_POSITIONS", 1),
            strategy_pivot_left=_int("STRATEGY_PIVOT_LEFT", 2),
            strategy_pivot_right=_int("STRATEGY_PIVOT_RIGHT", 2),
            strategy_atr_period=_int("STRATEGY_ATR_PERIOD", 14, 2),
            strategy_displacement_atr=_float("STRATEGY_DISPLACEMENT_ATR", 0.6, 0.01),
            strategy_max_bars_after_sweep=_int("STRATEGY_MAX_BARS_AFTER_SWEEP", 8),
            strategy_min_rr=_float("STRATEGY_MIN_RR", 1.5, 0.1),
            strategy_stop_buffer_atr=_float("STRATEGY_STOP_BUFFER_ATR", 0.1, 0.0),
            strategy_signal_expiry_minutes=_int("STRATEGY_SIGNAL_EXPIRY_MINUTES", 60),
            webhook_max_body_bytes=_int("WEBHOOK_MAX_BODY_BYTES", 1_048_576, 1024),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.symbols:
            raise ValueError("SYMBOLS must contain at least one instrument.")
        if any(not re.fullmatch(r"[A-Z0-9]{2,30}", symbol) for symbol in self.symbols):
            raise ValueError("SYMBOLS entries must contain only 2-30 alphanumeric characters.")
        if self.risk_per_trade_pct > 0.05:
            raise ValueError("RISK_PER_TRADE_PCT above 0.05 (5%) is rejected as an unsafe default.")
        if self.backtest_fixed_risk_usd <= 0 or not math.isfinite(self.backtest_fixed_risk_usd):
            raise ValueError("BACKTEST_FIXED_RISK_USD must be finite and positive.")
        if self.webhook_max_body_bytes < 1024:
            raise ValueError("WEBHOOK_MAX_BODY_BYTES must be at least 1024 bytes.")
        if self.enable_whatsapp:
            missing = []
            if not self.meta_access_token: missing.append("META_ACCESS_TOKEN")
            if not self.meta_phone_number_id: missing.append("META_PHONE_NUMBER_ID")
            if not self.meta_app_secret: missing.append("META_APP_SECRET")
            if not self.meta_verify_token: missing.append("META_VERIFY_TOKEN")
            if not self.whatsapp_recipients: missing.append("WHATSAPP_RECIPIENT_ALLOWLIST")
            for name, value in (("META_ACCESS_TOKEN", self.meta_access_token), ("META_PHONE_NUMBER_ID", self.meta_phone_number_id),
                                ("META_APP_SECRET", self.meta_app_secret), ("META_VERIFY_TOKEN", self.meta_verify_token)):
                if value.startswith("REPLACE_WITH_"):
                    missing.append(name)
            if missing:
                raise ValueError("WhatsApp enabled but required configuration is missing or still a placeholder: " + ", ".join(sorted(set(missing))))
            if not self.meta_phone_number_id.isdigit():
                raise ValueError("META_PHONE_NUMBER_ID must contain digits only.")
            normalized_recipients = [re.sub(r"\D", "", r) for r in self.whatsapp_recipients]
            invalid_recipients = [r for r in normalized_recipients if not 8 <= len(r) <= 15]
            if invalid_recipients:
                raise ValueError("WHATSAPP_RECIPIENT_ALLOWLIST contains an invalid recipient; use country code and 8-15 digits.")
            if len(normalized_recipients) != len(set(normalized_recipients)):
                raise ValueError("WHATSAPP_RECIPIENT_ALLOWLIST contains duplicate numbers after normalization.")
        if self.enable_telegram:
            missing_telegram = []
            if not self.telegram_bot_token or self.telegram_bot_token.startswith("REPLACE_WITH_"):
                missing_telegram.append("TELEGRAM_BOT_TOKEN")
            if not self.telegram_chat_ids:
                missing_telegram.append("TELEGRAM_CHAT_ID_ALLOWLIST")
            if missing_telegram:
                raise ValueError("Telegram enabled but required configuration is missing or still a placeholder: " + ", ".join(missing_telegram))
            from app.integrations.telegram import normalize_chat_id
            normalized_chat_ids = [normalize_chat_id(chat_id) for chat_id in self.telegram_chat_ids]
            if len(normalized_chat_ids) != len(set(normalized_chat_ids)):
                raise ValueError("TELEGRAM_CHAT_ID_ALLOWLIST contains duplicate chat IDs")
        if not self.meta_graph_api_version.startswith("v"):
            raise ValueError("META_GRAPH_API_VERSION must look like vNN.N.")

    def ensure_database_parent(self) -> None:
        Path(self.database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
