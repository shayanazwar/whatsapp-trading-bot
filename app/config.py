from __future__ import annotations

from functools import lru_cache

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

    # Telegram Bot API
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_users: str = Field(default="", alias="TELEGRAM_ALLOWED_USERS")
    telegram_webhook_url: str = Field(default="", alias="TELEGRAM_WEBHOOK_URL")
    telegram_webhook_secret: str = Field(default="", alias="TELEGRAM_WEBHOOK_SECRET")

    # Runtime / persistence
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    database_path: str = Field(default="signals.db", alias="DATABASE_PATH")
    alert_check_seconds: float = Field(default=0.75, alias="ALERT_CHECK_SECONDS")
    market_cache_retry_seconds: int = Field(default=5, alias="MARKET_CACHE_RETRY_SECONDS")
    discovery_refresh_seconds: int = Field(default=900, alias="DISCOVERY_REFRESH_SECONDS")
    chart_default_bars: int = Field(default=180, alias="CHART_DEFAULT_BARS")
    force_utc: bool = Field(default=True, alias="FORCE_UTC")

    # MEXC Futures automation
    mexc_api_base_url: str = Field(default="https://api.mexc.com", alias="MEXC_API_BASE_URL")
    mexc_access_key: str = Field(default="", alias="MEXC_ACCESS_KEY")
    mexc_secret_key: str = Field(default="", alias="MEXC_SECRET_KEY")
    mexc_recv_window: int = Field(default=10, alias="MEXC_RECV_WINDOW")
    mexc_order_type: int = Field(default=5, alias="MEXC_ORDER_TYPE")
    mexc_open_type: int = Field(default=1, alias="MEXC_OPEN_TYPE")
    mexc_default_leverage: int = Field(default=3, alias="MEXC_DEFAULT_LEVERAGE")
    max_risk_per_trade: float = Field(default=1.0, alias="MAX_RISK_PER_TRADE")
    max_open_trades: int = Field(default=3, alias="MAX_OPEN_TRADES")
    max_daily_loss: float = Field(default=0.05, alias="MAX_DAILY_LOSS")

    # Separate virtual-money paper trading. This never submits exchange orders.
    paper_trading_enabled: bool = Field(default=False, alias="PAPER_TRADING_ENABLED")
    paper_initial_balance: float = Field(default=100.0, alias="PAPER_INITIAL_BALANCE")
    paper_margin_percent: float = Field(default=1.0, alias="PAPER_MARGIN_PERCENT")
    paper_leverage: int = Field(default=20, alias="PAPER_LEVERAGE")
    paper_max_open_trades: int = Field(default=3, alias="PAPER_MAX_OPEN_TRADES")
    paper_poll_seconds: int = Field(default=15, alias="PAPER_POLL_SECONDS")
    paper_fee_rate: float = Field(default=0.0006, alias="PAPER_FEE_RATE")
    paper_slippage_bps: float = Field(default=2.0, alias="PAPER_SLIPPAGE_BPS")

    # Scanner switches
    scanner_enabled: bool = Field(default=False, alias="SCANNER_ENABLED")
    auto_signal_enabled: bool = Field(default=False, alias="AUTO_SIGNAL_ENABLED")
    auto_trade_enabled: bool = Field(default=False, alias="AUTO_TRADE_ENABLED")
    scan_interval_seconds: int = Field(default=3600, alias="SCAN_INTERVAL_SECONDS")
    max_symbols: int = Field(default=300, alias="MAX_SYMBOLS")
    scan_concurrency: int = Field(default=4, alias="SCAN_CONCURRENCY")
    mexc_public_min_interval_seconds: float = Field(default=0.20, alias="MEXC_PUBLIC_MIN_INTERVAL_SECONDS")
    mexc_public_window_seconds: float = Field(default=2.0, alias="MEXC_PUBLIC_WINDOW_SECONDS")
    mexc_public_window_limit: int = Field(default=8, alias="MEXC_PUBLIC_WINDOW_LIMIT")
    mexc_rate_limit_max_retries: int = Field(default=4, alias="MEXC_RATE_LIMIT_MAX_RETRIES")
    mexc_rate_limit_backoff_seconds: float = Field(default=2.0, alias="MEXC_RATE_LIMIT_BACKOFF_SECONDS")
    mexc_rate_limit_backoff_cap_seconds: float = Field(default=20.0, alias="MEXC_RATE_LIMIT_BACKOFF_CAP_SECONDS")
    mexc_rate_limit_jitter_seconds: float = Field(default=0.25, alias="MEXC_RATE_LIMIT_JITTER_SECONDS")
    candle_limit: int = Field(default=250, alias="CANDLE_LIMIT")
    min_confluence: int = Field(default=0, alias="MIN_CONFLUENCE")
    min_rr: float = Field(default=1.6, alias="MIN_RR")
    max_entry_drift_pct: float = Field(default=0.002, alias="MAX_ENTRY_DRIFT_PCT")
    estimated_round_trip_cost_pct: float = Field(default=0.0012, alias="ESTIMATED_ROUND_TRIP_COST_PCT")
    estimated_funding_cost_pct: float = Field(default=0.0002, alias="ESTIMATED_FUNDING_COST_PCT")
    max_mexc_spread_pct: float = Field(default=0.001, alias="MAX_MEXC_SPREAD_PCT")
    max_index_dislocation_pct: float = Field(default=0.002, alias="MAX_INDEX_DISLOCATION_PCT")
    max_data_age_seconds: float = Field(default=5.0, alias="MAX_DATA_AGE_SECONDS")
    orderbook_levels: int = Field(default=10, alias="ORDERBOOK_LEVELS")
    trade_flow_limit: int = Field(default=100, alias="TRADE_FLOW_LIMIT")
    require_increasing_volume: bool = Field(default=False, alias="REQUIRE_INCREASING_VOLUME")
    signal_expiry_minutes: int = Field(default=30, alias="SIGNAL_EXPIRY_MINUTES")
    # Signal freshness is measured from the CLOSE of the completed 1H entry candle.
    max_signal_age_seconds: float = Field(default=120.0, alias="MAX_SIGNAL_AGE_SECONDS")
    auto_signal_recipients: str = Field(default="", alias="AUTO_SIGNAL_RECIPIENTS")
    auto_signal_telegram_recipients: str = Field(
        default="",
        alias="AUTO_SIGNAL_TELEGRAM_RECIPIENTS",
    )
    test_symbols: str = Field(default="", alias="TEST_SYMBOLS")

    # Backtest resource controls
    backtest_max_symbols: int = Field(default=200, alias="BACKTEST_MAX_SYMBOLS")
    backtest_symbol_concurrency: int = Field(default=2, alias="BACKTEST_SYMBOL_CONCURRENCY")
    backtest_analysis_timeout_seconds: float = Field(default=120.0, alias="BACKTEST_ANALYSIS_TIMEOUT_SECONDS")
    backtest_child_boot_timeout_seconds: float = Field(default=30.0, alias="BACKTEST_CHILD_BOOT_TIMEOUT_SECONDS")
    backtest_progress_interval_seconds: float = Field(default=5.0, alias="BACKTEST_PROGRESS_INTERVAL_SECONDS")
    backtest_max_open_positions: int = Field(default=4, alias="BACKTEST_MAX_OPEN_POSITIONS")
    backtest_max_same_direction: int = Field(default=2, alias="BACKTEST_MAX_SAME_DIRECTION")
    backtest_total_open_risk_r: float = Field(default=3.0, alias="BACKTEST_TOTAL_OPEN_RISK_R")
    backtest_fee_rate: float = Field(default=0.0006, alias="BACKTEST_FEE_RATE")
    backtest_slippage_bps: float = Field(default=2.0, alias="BACKTEST_SLIPPAGE_BPS")

    # Additional live-trading kill switch
    allow_live_execution: bool = Field(default=False, alias="ALLOW_LIVE_EXECUTION")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @property
    def backtest_execution_cost_pct(self) -> float:
        """Round-trip fee + adverse slippage allowance used by the paper simulator."""
        fee = max(0.0, float(self.backtest_fee_rate)) * 2.0
        slippage = max(0.0, float(self.backtest_slippage_bps)) / 10_000.0 * 2.0
        return fee + slippage

    @property
    def effective_round_trip_cost_pct(self) -> float:
        """One conservative cost model shared by live validation and backtests."""
        execution_cost = self.backtest_execution_cost_pct
        configured_cost = max(0.0, float(self.estimated_round_trip_cost_pct))
        funding = max(0.0, float(self.estimated_funding_cost_pct))
        return max(configured_cost, execution_cost) + funding

    @property
    def allowed_user_set(self) -> set[str]:
        return {
            value.strip().replace("+", "")
            for value in self.allowed_users.split(",")
            if value.strip()
        }

    @property
    def telegram_allowed_user_set(self) -> set[str]:
        return {
            f"tg:{value.strip()}"
            for value in self.telegram_allowed_users.split(",")
            if value.strip()
        }

    @property
    def auto_signal_recipient_set(self) -> set[str]:
        whatsapp = {
            value.strip().replace("+", "")
            for value in self.auto_signal_recipients.split(",")
            if value.strip()
        }
        telegram = {
            f"tg:{value.strip()}"
            for value in self.auto_signal_telegram_recipients.split(",")
            if value.strip()
        }

        if not whatsapp:
            whatsapp = self.allowed_user_set
        if not telegram:
            telegram = self.telegram_allowed_user_set
        return whatsapp | telegram

    @property
    def test_symbol_list(self) -> list[str]:
        return [
            value.strip().upper()
            for value in self.test_symbols.split(",")
            if value.strip()
        ]


def validate_whatsapp_settings(settings: Settings) -> None:
    """Fail startup with a clear configuration error when WhatsApp is incomplete."""
    required = {
        "META_ACCESS_TOKEN": settings.meta_access_token,
        "META_PHONE_NUMBER_ID": settings.meta_phone_number_id,
        "META_VERIFY_TOKEN": settings.meta_verify_token,
        "META_APP_SECRET": settings.meta_app_secret,
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(
            "Missing required WhatsApp configuration: " + ", ".join(missing)
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
