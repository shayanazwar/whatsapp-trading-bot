from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_secret_key: str = "development-only-change-me"
    admin_username: str = "admin"
    admin_password: str = "change-this-local-password"
    session_max_age_seconds: int = 43_200
    database_url: str = "sqlite:///./data/kronos_intelligence.db"
    paper_mode: bool = True
    live_trading_enabled: bool = False
    automatic_order_execution: bool = False

    mexc_api_base_url: str = "https://api.mexc.com"
    mexc_ws_url: str = "wss://contract.mexc.com/edge"
    mexc_http_timeout: float = 15.0
    mexc_max_retries: int = 3
    mexc_request_min_interval_seconds: float = 0.11

    auto_scan_enabled: bool = True
    market_scan_interval_seconds: int = 3600
    universe_limit: int = 30
    min_24h_turnover_usdt: float = 1_000_000
    max_spread_bps: float = 15
    min_net_rr: float = 1.5
    default_account_equity: float = 1000
    risk_per_trade_pct: float = 0.5
    max_portfolio_risk_pct: float = 3.0
    default_leverage: int = 2

    kronos_repo_url: str = "https://github.com/shiyu-coder/Kronos.git"
    kronos_repo_revision: str = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
    kronos_model_id: str = "NeoQuasar/Kronos-small"
    kronos_model_revision: str = "901c26c1332695a2a8f243eb2f37243a37bea320"
    kronos_tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base"
    kronos_tokenizer_revision: str = "f6c7de7b0490e422fde7a44bc73369e07115c445"
    kronos_device: str = "auto"
    kronos_context_length: int = 512
    forecast_sample_count: int = 1000
    forecast_horizon: int = 24
    forecast_chunk_size: int = 16
    forecast_temperature: float = 1.0
    forecast_top_p: float = 0.9
    forecast_random_seed: int = 42

    simulation_replays: int = 10_000
    simulation_block_length: int = 4
    backtest_max_days: int = 180
    backtest_default_equity: float = 1000
    backtest_risk_per_trade_pct: float = 0.5
    backtest_taker_fee_rate: float | None = None
    backtest_maker_fee_rate: float | None = None
    backtest_slippage_bps: float = 3

    worker_role: str = "all"
    worker_poll_seconds: float = 2
    job_max_attempts: int = 3
    job_lease_seconds: int = 900



    @field_validator("mexc_api_base_url")
    @classmethod
    def clean_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def normalize_database_driver(cls, value: str) -> str:
        # Render commonly supplies postgresql://; this project installs psycopg v3.
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value[len("postgres://"):]
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value[len("postgresql://"):]
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def data_dir(self) -> Path:
        path = Path("data")
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if settings.live_trading_enabled or settings.automatic_order_execution or not settings.paper_mode:
        raise RuntimeError(
            "This release is research/paper-only. PAPER_MODE must be true and live/automatic execution false."
        )
    if settings.is_production:
        if settings.app_secret_key == "development-only-change-me" or len(settings.app_secret_key) < 32:
            raise RuntimeError("APP_SECRET_KEY must be a unique random secret of at least 32 characters")
        if settings.admin_password == "change-this-local-password" or len(settings.admin_password) < 14:
            raise RuntimeError("Set a strong ADMIN_PASSWORD (minimum 14 characters) in production")
    return settings
