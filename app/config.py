from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_env() -> None:
    """Load a simple project .env file without overriding platform environment variables."""
    path = Path(".env")
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


_load_local_env()


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be numeric") from exc


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _csv(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    database_path: str = os.getenv("DATABASE_PATH", "./data/btc_mexc_bot.sqlite3")
    monitor_enabled: bool = os.getenv("MONITOR_ENABLED", "false").lower() in {"1", "true", "yes"}
    mexc_base_url: str = os.getenv("MEXC_BASE_URL", "https://api.mexc.com").rstrip("/")
    mexc_symbol: str = os.getenv("MEXC_SYMBOL", "BTC_USDT")
    mexc_interval: str = os.getenv("MEXC_INTERVAL", "Min1")
    mexc_timeout_seconds: float = _float("MEXC_TIMEOUT_SECONDS", 15)
    mexc_max_retries: int = _int("MEXC_MAX_RETRIES", 4)

    ema_fast: int = _int("EMA_FAST", 6)
    ema_mid: int = _int("EMA_MID", 12)
    ema_slow: int = _int("EMA_SLOW", 21)
    live_rsi_period: int = _int("LIVE_RSI_PERIOD", 9)
    live_rsi_upper: float = _float("LIVE_RSI_UPPER", 65)
    live_rsi_lower: float = _float("LIVE_RSI_LOWER", 35)
    warmup_candles: int = _int("WARMUP_CANDLES", 120)
    live_tp_move_pct: float = _float("LIVE_TP_MOVE_PCT", 0.50)

    abnormal_5m_move_pct: float = _float("ABNORMAL_5M_MOVE_PCT", 1.00)
    abnormal_1m_range_pct: float = _float("ABNORMAL_1M_RANGE_PCT", 0.60)
    recovery_5m_move_pct: float = _float("RECOVERY_5M_MOVE_PCT", 0.50)
    recovery_minutes: int = _int("RECOVERY_MINUTES", 15)
    signal_expiry_deviation_pct: float = _float("SIGNAL_EXPIRY_DEVIATION_PCT", 0.20)

    starting_balance_usdt: float = _float("STARTING_BALANCE_USDT", 100)
    max_margin_usdt: float = _float("MAX_MARGIN_USDT", 0.50)
    model_leverage: float = _float("MODEL_LEVERAGE", 200)
    backtest_taker_fee_rate: float = _float("BACKTEST_TAKER_FEE_RATE", 0.0008)
    backtest_slippage_ticks: int = _int("BACKTEST_SLIPPAGE_TICKS", 1)
    backtest_tp_pcts: tuple[float, ...] = tuple(float(x) for x in os.getenv("BACKTEST_TP_PCTS", "0.50,0.75,1.00").split(",") if x.strip())

    graph_api_version: str = os.getenv("GRAPH_API_VERSION", "v26.0")
    wa_phone_number_id: str = os.getenv("WA_PHONE_NUMBER_ID", "")
    wa_access_token: str = os.getenv("WA_ACCESS_TOKEN", "")
    wa_verify_token: str = os.getenv("WA_VERIFY_TOKEN", "")
    wa_app_secret: str = os.getenv("WA_APP_SECRET", "")
    wa_recipients: tuple[str, ...] = _csv("WA_RECIPIENTS")
    authorized_wa_ids: tuple[str, ...] = _csv("AUTHORIZED_WA_IDS")

    def validate(self) -> None:
        if self.mexc_symbol != "BTC_USDT":
            raise ValueError("This release is restricted to MEXC BTC_USDT")
        if self.mexc_interval != "Min1":
            raise ValueError("This release is restricted to Min1 candles")
        if not (0 < self.max_margin_usdt <= 0.50):
            raise ValueError("MAX_MARGIN_USDT must be > 0 and <= 0.50")
        if self.model_leverage <= 0:
            raise ValueError("MODEL_LEVERAGE must be positive")
        if min(self.ema_fast, self.ema_mid, self.ema_slow, self.live_rsi_period) < 2:
            raise ValueError("Indicator periods must be >= 2")
        if self.warmup_candles < 100:
            raise ValueError("WARMUP_CANDLES must be at least 100")
        if not (self.ema_fast < self.ema_mid < self.ema_slow):
            raise ValueError("EMA periods must be ordered fast < mid < slow")
        if not (0 < self.live_rsi_lower < self.live_rsi_upper < 100):
            raise ValueError("Invalid RSI thresholds")
        if self.live_tp_move_pct <= 0:
            raise ValueError("LIVE_TP_MOVE_PCT must be positive")
        if not self.backtest_tp_pcts or any(x <= 0 or x > 10 for x in self.backtest_tp_pcts):
            raise ValueError("Backtest TP percentages must be between 0 and 10")
        if self.backtest_taker_fee_rate < 0 or self.backtest_slippage_ticks < 0:
            raise ValueError("Fees and slippage cannot be negative")
        if self.mexc_max_retries < 0:
            raise ValueError("MEXC_MAX_RETRIES cannot be negative")
        if self.mexc_timeout_seconds <= 0 or self.starting_balance_usdt <= 0:
            raise ValueError("API timeout and planning balance must be positive")
        if min(self.abnormal_5m_move_pct, self.abnormal_1m_range_pct,
               self.recovery_5m_move_pct, self.signal_expiry_deviation_pct) <= 0:
            raise ValueError("Abnormal-move and stale-signal thresholds must be positive")
        if self.recovery_minutes < 1:
            raise ValueError("RECOVERY_MINUTES must be at least 1")
        for number in (*self.wa_recipients, *self.authorized_wa_ids):
            if not number.isdigit():
                raise ValueError("WhatsApp recipient and authorized sender IDs must contain digits only")
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
