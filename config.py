from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Meta / WhatsApp Cloud API
    meta_access_token: str = Field(default="", alias="META_ACCESS_TOKEN")
    meta_phone_number_id: str = Field(default="", alias="META_PHONE_NUMBER_ID")
    meta_verify_token: str = Field(default="", alias="META_VERIFY_TOKEN")
    meta_app_secret: str = Field(default="", alias="META_APP_SECRET")
    meta_graph_version: str = Field(default="v26.0", alias="META_GRAPH_VERSION")
    allowed_users: str = Field(default="", alias="ALLOWED_USERS")

    # Runtime / persistence
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    database_path: str = Field(default="signals.db", alias="DATABASE_PATH")

    # Existing Binance alert/chart path. Kept intact for existing commands.
    binance_ws_url: str = Field(
        default="wss://data-stream.binance.vision/ws/!miniTicker@arr",
        alias="BINANCE_WS_URL",
    )
    binance_rest_url: str = Field(
        default="https://data-api.binance.vision",
        alias="BINANCE_REST_URL",
    )
    alert_check_seconds: float = Field(default=0.75, alias="ALERT_CHECK_SECONDS")
    market_cache_retry_seconds: int = Field(default=5, alias="MARKET_CACHE_RETRY_SECONDS")

    discovery_exchanges: str = Field(
        default="binance,bybit,okx,gateio,kucoin",
        alias="DISCOVERY_EXCHANGES",
    )
    discovery_refresh_seconds: int = Field(
        default=21600,
        alias="DISCOVERY_REFRESH_SECONDS",
    )
    chart_default_bars: int = Field(default=180, alias="CHART_DEFAULT_BARS")
    force_utc: bool = Field(default=True, alias="FORCE_UTC")

    # MEXC Futures automation.
    # Current official Futures API base: https://api.mexc.com
    mexc_api_base_url: str = Field(
        default="https://api.mexc.com",
        alias="MEXC_API_BASE_URL",
    )
    mexc_access_key: str = Field(default="", alias="MEXC_ACCESS_KEY")
    mexc_secret_key: str = Field(default="", alias="MEXC_SECRET_KEY")
    mexc_recv_window: int = Field(default=10, alias="MEXC_RECV_WINDOW")
    mexc_order_type: int = Field(default=1, alias="MEXC_ORDER_TYPE")
    mexc_open_type: int = Field(default=1, alias="MEXC_OPEN_TYPE")  # isolated
    mexc_default_leverage: int = Field(default=3, alias="MEXC_DEFAULT_LEVERAGE")
    max_risk_per_trade: float = Field(default=0.01, alias="MAX_RISK_PER_TRADE")
    max_open_trades: int = Field(default=3, alias="MAX_OPEN_TRADES")
    max_daily_loss: float = Field(default=0.05, alias="MAX_DAILY_LOSS")

    # Scanner switches.
    scanner_enabled: bool = Field(default=False, alias="SCANNER_ENABLED")
    auto_signal_enabled: bool = Field(default=False, alias="AUTO_SIGNAL_ENABLED")
    auto_trade_enabled: bool = Field(default=False, alias="AUTO_TRADE_ENABLED")
    scan_interval_seconds: int = Field(default=60, alias="SCAN_INTERVAL_SECONDS")
    max_symbols: int = Field(default=100, alias="MAX_SYMBOLS")
    scan_concurrency: int = Field(default=4, alias="SCAN_CONCURRENCY")
    candle_limit: int = Field(default=200, alias="CANDLE_LIMIT")
    min_confluence: int = Field(default=5, alias="MIN_CONFLUENCE")
    min_rr: float = Field(default=2.0, alias="MIN_RR")
    require_increasing_volume: bool = Field(
        default=False,
        alias="REQUIRE_INCREASING_VOLUME",
    )
    signal_expiry_minutes: int = Field(default=30, alias="SIGNAL_EXPIRY_MINUTES")
    auto_signal_recipients: str = Field(default="", alias="AUTO_SIGNAL_RECIPIENTS")
    test_symbols: str = Field(default="", alias="TEST_SYMBOLS")

    # Additional live-trading kill switch. Must remain false until live execution
    # has been explicitly tested with the user's account and approved.
    allow_live_execution: bool = Field(
        default=False,
        alias="ALLOW_LIVE_EXECUTION",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @property
    def allowed_user_set(self) -> set[str]:
        return {
            value.strip().replace("+", "")
            for value in self.allowed_users.split(",")
            if value.strip()
        }

    @property
    def auto_signal_recipient_set(self) -> set[str]:
        configured = {
            value.strip().replace("+", "")
            for value in self.auto_signal_recipients.split(",")
            if value.strip()
        }
        return configured or self.allowed_user_set

    @property
    def discovery_exchange_list(self) -> List[str]:
        return [
            value.strip().lower()
            for value in self.discovery_exchanges.split(",")
            if value.strip()
        ]

    @property
    def test_symbol_list(self) -> list[str]:
        return [
            value.strip().upper()
            for value in self.test_symbols.split(",")
            if value.strip()
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
