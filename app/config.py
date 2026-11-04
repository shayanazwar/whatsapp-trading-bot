from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
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
    max_book_distance_bps: float = 25
    min_book_depth_multiple: float = Field(default=1.0, gt=0.0, le=100.0)
    max_signal_market_data_age_seconds: float = 15
    max_signal_entry_drift_atr: float = 0.5
    min_net_rr: float = 1.5
    tp1_exit_fraction: float = Field(default=0.5, gt=0.0, lt=1.0)
    tp2_r_multiple: float = Field(default=2.5, gt=1.0, le=10.0)
    default_account_equity: float = 1000
    risk_per_trade_pct: float = 0.5
    margin_buffer_pct: float = 10.0
    max_portfolio_risk_pct: float = 3.0
    max_portfolio_notional_multiple: float = Field(default=3.0, gt=0.0, le=20.0)
    default_leverage: int = Field(default=2, ge=1, le=500)

    kronos_repo_url: str = "https://github.com/shiyu-coder/Kronos.git"
    kronos_repo_revision: str = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
    kronos_model_id: str = "NeoQuasar/Kronos-small"
    kronos_model_revision: str = "901c26c1332695a2a8f243eb2f37243a37bea320"
    kronos_tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base"
    kronos_tokenizer_revision: str = "f6c7de7b0490e422fde7a44bc73369e07115c445"
    kronos_device: str = "auto"
    kronos_context_length: int = 512
    forecast_sample_count: int = Field(default=1000, ge=1, le=1000)
    forecast_horizon: int = Field(default=24, ge=1, le=48)
    forecast_chunk_size: int = Field(default=16, ge=1, le=256)
    forecast_temperature: float = Field(default=1.0, gt=0.0, le=5.0)
    forecast_top_p: float = Field(default=0.9, gt=0.0, le=1.0)
    forecast_random_seed: int = 42
    forecast_max_invalid_bar_fraction: float = Field(default=0.005, ge=0.0, le=0.05)
    forecast_min_expected_r_before_funding: float = 0.05

    simulation_replays: int = Field(default=10_000, ge=100, le=100_000)
    simulation_block_length: int = Field(default=8, ge=2, le=128)
    backtest_max_days: int = Field(default=180, ge=1, le=365)
    backtest_default_equity: float = 1000
    backtest_risk_per_trade_pct: float = 0.5
    backtest_taker_fee_rate: float | None = None
    backtest_slippage_bps: float = 3

    worker_role: str = "all"
    worker_poll_seconds: float = 2
    job_max_attempts: int = 3
    job_lease_seconds: int = 900

    # WhatsApp Cloud API. Secrets are supplied through Render environment variables.
    whatsapp_enabled: bool = False
    whatsapp_access_token: str = Field(
        default="", validation_alias=AliasChoices("META_ACCESS_TOKEN", "WHATSAPP_ACCESS_TOKEN")
    )
    whatsapp_phone_number_id: str = Field(
        default="", validation_alias=AliasChoices("META_PHONE_NUMBER_ID", "WHATSAPP_PHONE_NUMBER_ID")
    )
    whatsapp_verify_token: str = Field(
        default="", validation_alias=AliasChoices("META_VERIFY_TOKEN", "WHATSAPP_VERIFY_TOKEN")
    )
    whatsapp_app_secret: str = Field(
        default="", validation_alias=AliasChoices("META_APP_SECRET", "WHATSAPP_APP_SECRET")
    )
    whatsapp_graph_api_version: str = "v26.0"
    whatsapp_api_timeout_seconds: float = Field(default=15.0, gt=1.0, le=60.0)
    whatsapp_api_max_retries: int = Field(default=2, ge=0, le=4)
    whatsapp_allowed_recipients: str = ""
    whatsapp_default_recipient: str = ""
    public_base_url: str = Field(
        default="", validation_alias=AliasChoices("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL")
    )



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
