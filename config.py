from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    meta_access_token: str = Field(default="", alias="META_ACCESS_TOKEN")
    meta_phone_number_id: str = Field(default="", alias="META_PHONE_NUMBER_ID")
    meta_verify_token: str = Field(default="", alias="META_VERIFY_TOKEN")
    meta_app_secret: str = Field(default="", alias="META_APP_SECRET")
    meta_graph_version: str = Field(default="v26.0", alias="META_GRAPH_VERSION")

    allowed_users: str = Field(default="", alias="ALLOWED_USERS")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    binance_ws_url: str = Field(
        default="wss://data-stream.binance.vision/ws/!miniTicker@arr",
        alias="BINANCE_WS_URL",
    )
    binance_rest_url: str = Field(
        default="https://data-api.binance.vision", alias="BINANCE_REST_URL"
    )
    alert_check_seconds: float = Field(default=0.75, alias="ALERT_CHECK_SECONDS")
    market_cache_retry_seconds: int = Field(default=5, alias="MARKET_CACHE_RETRY_SECONDS")

    discovery_exchanges: str = Field(
        default="binance,bybit,okx,gateio,kucoin", alias="DISCOVERY_EXCHANGES"
    )
    discovery_refresh_seconds: int = Field(default=21600, alias="DISCOVERY_REFRESH_SECONDS")
    chart_default_bars: int = Field(default=180, alias="CHART_DEFAULT_BARS")
    force_utc: bool = Field(default=True, alias="FORCE_UTC")

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
    def discovery_exchange_list(self) -> List[str]:
        return [
            value.strip().lower()
            for value in self.discovery_exchanges.split(",")
            if value.strip()
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
