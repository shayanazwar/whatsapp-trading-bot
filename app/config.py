"""Validated environment configuration. Secret values are never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None or raw.strip() == "" else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not (value == value and abs(value) != float("inf")):
        raise ValueError(f"{name} must be finite")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None or raw.strip() == "" else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    database_path: Path
    database_url: str
    reports_dir: Path
    mexc_rest_base_url: str
    mexc_price_series: str
    http_timeout_seconds: float
    http_max_retries: int
    candle_close_grace_seconds: int
    scheduler_enabled: bool
    scheduler_interval_seconds: int
    paper_trading_enabled: bool
    data_alignment_probe_approved: bool
    auto_trading_enabled: bool
    live_execution_enabled: bool
    live_execution_build_released: bool
    default_symbols: tuple[str, ...]
    mexc_universe_top_n: int
    mexc_universe_scan_enabled: bool
    market_data_dir: Path
    signal_push_enabled: bool
    signal_ttl_bars: int
    signal_min_score: float
    signal_push_min_score: float
    candidate_stop_atr_multiple: float
    candidate_target_atr_multiple: float
    candidate_min_net_rr: float
    mtf_htf_warmup_bars: int
    live_max_spread_atr_fraction: float
    live_min_orderbook_depth_usdt: float
    max_adverse_funding_rate: float
    backtest_min_coverage_pct: float
    backtest_min_universe_coverage_pct: float
    strategy_version: str
    risk_per_trade_fraction: float
    max_open_risk_fraction: float
    max_drawdown_fraction: float
    max_daily_loss_fraction: float
    max_concurrent_positions: int
    fixed_tp_pct: float
    fixed_sl_pct: float
    fixed_win_payout: float
    fixed_loss_payout: float
    default_starting_balance: float
    initial_margin_usdt: float
    max_leverage: int
    net_take_profit_usdt: float
    requested_stop_loss_usdt: float
    backtest_margin_mode: str
    backtest_max_holding_minutes: int
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    ema_fast: int
    ema_mid: int
    ema_slow: int
    rsi_volume_period: int
    rsi_volume_multiplier: float
    exchange_taker_fee_rate: float
    exchange_maker_fee_rate: float
    backtest_slippage_bps_per_side: float
    backtest_max_positions: int
    backtest_max_open_risk_fraction: float
    cost_stress_multipliers: tuple[float, ...]
    whatsapp_crypto_messaging_allowed: bool
    whatsapp_policy_clearance_ref: str
    meta_verify_token: str
    meta_app_secret: str
    meta_access_token: str
    meta_phone_number_id: str
    whatsapp_allowed_senders: tuple[str, ...]
    whatsapp_monthly_message_cap: int
    web_api_token: str
    mexc_api_key: str
    mexc_api_secret: str

    @property
    def whatsapp_policy_gate_open(self) -> bool:
        """A configuration gate only; it is not proof that Meta granted permission."""
        return bool(
            self.whatsapp_crypto_messaging_allowed
            and self.whatsapp_policy_clearance_ref.strip()
        )


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    base_url = os.getenv("MEXC_REST_BASE_URL", "https://api.mexc.com").rstrip("/")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "api.mexc.com" or parsed_url.port not in (None, 443):
        raise ValueError("MEXC_REST_BASE_URL must be exactly https://api.mexc.com")

    symbols_raw = os.getenv("DEFAULT_SYMBOLS", "BTC_USDT,ETH_USDT,SOL_USDT")
    from app.symbols import normalize_symbol

    symbols = tuple(dict.fromkeys(normalize_symbol(x) for x in symbols_raw.split(",") if x.strip()))
    if not symbols:
        raise ValueError("DEFAULT_SYMBOLS must contain at least one valid symbol")

    stress_raw = os.getenv("BACKTEST_COST_STRESS_MULTIPLIERS", "1.0,1.5,2.0")
    try:
        stresses = tuple(float(x.strip()) for x in stress_raw.split(",") if x.strip())
    except ValueError as exc:
        raise ValueError("BACKTEST_COST_STRESS_MULTIPLIERS must be comma-separated numbers") from exc
    if not stresses or any(x < 1 or not (x == x and abs(x) != float("inf")) for x in stresses):
        raise ValueError("Cost stress multipliers must be finite numbers >= 1")

    app_role = os.getenv("APP_ROLE", "web").strip().lower()
    if app_role not in {"web", "worker"}:
        raise ValueError("APP_ROLE must be web or worker")
    # Render's persistent disk is attached only to the background worker. The web
    # service must never try to create or write into the worker-only /var/data mount.
    default_data_root = Path("/var/data") if app_role == "worker" else Path("./data")
    settings = Settings(
        app_env=os.getenv("APP_ENV", "development"),
        database_path=Path(os.getenv("DATABASE_PATH", "./data/mexc_bot.sqlite3")),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        reports_dir=Path(os.getenv("REPORTS_DIR", str(default_data_root / "reports"))),
        mexc_rest_base_url=base_url,
        mexc_price_series=os.getenv("MEXC_PRICE_SERIES", "standard").strip().lower(),
        http_timeout_seconds=_float("HTTP_TIMEOUT_SECONDS", 12.0),
        http_max_retries=_int("HTTP_MAX_RETRIES", 3),
        candle_close_grace_seconds=_int("CANDLE_CLOSE_GRACE_SECONDS", 5),
        scheduler_enabled=_bool("SCHEDULER_ENABLED", False),
        scheduler_interval_seconds=_int("SCHEDULER_INTERVAL_SECONDS", 60),
        paper_trading_enabled=_bool("PAPER_TRADING_ENABLED", False),
        data_alignment_probe_approved=_bool("DATA_ALIGNMENT_PROBE_APPROVED", False),
        auto_trading_enabled=_bool("AUTO_TRADING_ENABLED", False),
        live_execution_enabled=_bool("LIVE_EXECUTION_ENABLED", False),
        live_execution_build_released=_bool("LIVE_EXECUTION_BUILD_RELEASED", False),
        default_symbols=symbols,
        mexc_universe_top_n=_int("MEXC_UNIVERSE_TOP_N", 100),
        mexc_universe_scan_enabled=_bool("MEXC_UNIVERSE_SCAN_ENABLED", False),
        market_data_dir=Path(os.getenv("MARKET_DATA_DIR", str(default_data_root / "market"))),
        signal_push_enabled=_bool("SIGNAL_PUSH_ENABLED", False),
        signal_ttl_bars=_int("SIGNAL_TTL_BARS", 2),
        signal_min_score=_float("SIGNAL_MIN_SCORE", 65.0),
        signal_push_min_score=_float("SIGNAL_PUSH_MIN_SCORE", 80.0),
        candidate_stop_atr_multiple=_float("CANDIDATE_STOP_ATR_MULTIPLE", 1.5),
        candidate_target_atr_multiple=_float("CANDIDATE_TARGET_ATR_MULTIPLE", 2.5),
        candidate_min_net_rr=_float("CANDIDATE_MIN_NET_RR", 1.2),
        mtf_htf_warmup_bars=_int("MTF_HTF_WARMUP_BARS", 250),
        live_max_spread_atr_fraction=_float("LIVE_MAX_SPREAD_ATR_FRACTION", 0.10),
        live_min_orderbook_depth_usdt=_float("LIVE_MIN_ORDERBOOK_DEPTH_USDT", 250.0),
        max_adverse_funding_rate=_float("MAX_ADVERSE_FUNDING_RATE", 0.0005),
        backtest_min_coverage_pct=_float("BACKTEST_MIN_COVERAGE_PCT", 98.0),
        backtest_min_universe_coverage_pct=_float("BACKTEST_MIN_UNIVERSE_COVERAGE_PCT", 90.0),
        strategy_version=os.getenv("STRATEGY_VERSION", "S1-RFPC-0.2.0-1H-trigger"),
        risk_per_trade_fraction=_float("RISK_PER_TRADE_FRACTION", 0.005),
        max_open_risk_fraction=_float("MAX_OPEN_RISK_FRACTION", 0.02),
        max_drawdown_fraction=_float("MAX_DRAWDOWN_FRACTION", 0.10),
        max_daily_loss_fraction=_float("MAX_DAILY_LOSS_FRACTION", 0.02),
        max_concurrent_positions=_int("MAX_CONCURRENT_POSITIONS", 1),
        fixed_tp_pct=_float("FIXED_TP_PCT", 5.0),
        fixed_sl_pct=_float("FIXED_SL_PCT", 2.0),
        fixed_win_payout=_float("FIXED_WIN_PAYOUT", 20.0),
        fixed_loss_payout=_float("FIXED_LOSS_PAYOUT", 5.0),
        default_starting_balance=_float("DEFAULT_STARTING_BALANCE", 100.0),
        initial_margin_usdt=_float("INITIAL_MARGIN_USDT", 1.0),
        max_leverage=_int("MAX_LEVERAGE", 50),
        net_take_profit_usdt=_float("NET_TAKE_PROFIT_USDT", 0.30),
        requested_stop_loss_usdt=_float("REQUESTED_STOP_LOSS_USDT", 3.0),
        backtest_margin_mode=os.getenv("BACKTEST_MARGIN_MODE", "cross").strip().lower(),
        backtest_max_holding_minutes=_int("BACKTEST_MAX_HOLDING_MINUTES", 1440),
        rsi_period=_int("RSI_PERIOD", 14),
        rsi_overbought=_float("RSI_OVERBOUGHT", 70.0),
        rsi_oversold=_float("RSI_OVERSOLD", 30.0),
        ema_fast=_int("EMA_FAST", 21),
        ema_mid=_int("EMA_MID", 50),
        ema_slow=_int("EMA_SLOW", 100),
        rsi_volume_period=_int("RSI_VOLUME_PERIOD", 20),
        rsi_volume_multiplier=_float("RSI_VOLUME_MULTIPLIER", 1.2),
        exchange_taker_fee_rate=_float("EXCHANGE_TAKER_FEE_RATE", 0.0008),
        exchange_maker_fee_rate=_float("EXCHANGE_MAKER_FEE_RATE", 0.0006),
        backtest_slippage_bps_per_side=_float("BACKTEST_SLIPPAGE_BPS_PER_SIDE", 5.0),
        backtest_max_positions=_int("BACKTEST_MAX_POSITIONS", 50),
        backtest_max_open_risk_fraction=_float("BACKTEST_MAX_OPEN_RISK_FRACTION", 0.25),
        cost_stress_multipliers=stresses,
        whatsapp_crypto_messaging_allowed=_bool("WHATSAPP_CRYPTO_MESSAGING_ALLOWED", False),
        whatsapp_policy_clearance_ref=os.getenv("WHATSAPP_POLICY_CLEARANCE_REF", ""),
        meta_verify_token=os.getenv("META_VERIFY_TOKEN", ""),
        meta_app_secret=os.getenv("META_APP_SECRET", ""),
        meta_access_token=os.getenv("META_ACCESS_TOKEN", ""),
        meta_phone_number_id=os.getenv("META_PHONE_NUMBER_ID", ""),
        whatsapp_allowed_senders=tuple(
            x.strip() for x in os.getenv("WHATSAPP_ALLOWED_SENDERS", "").split(",") if x.strip()
        ),
        whatsapp_monthly_message_cap=_int("WHATSAPP_MONTHLY_MESSAGE_CAP", 900),
        web_api_token=os.getenv("WEB_API_TOKEN", "").strip(),
        mexc_api_key=os.getenv("MEXC_API_KEY", ""),
        mexc_api_secret=os.getenv("MEXC_API_SECRET", ""),
    )
    if settings.mexc_price_series not in {"real", "standard"}:
        raise ValueError("MEXC_PRICE_SERIES must be real or standard")
    if not 5 <= settings.scheduler_interval_seconds <= 3600:
        raise ValueError("SCHEDULER_INTERVAL_SECONDS must be between 5 and 3600")
    if not 0 < settings.risk_per_trade_fraction <= 0.05:
        raise ValueError("RISK_PER_TRADE_FRACTION must be in (0, 0.05]")
    if settings.http_timeout_seconds <= 0 or settings.http_max_retries < 0:
        raise ValueError("HTTP timeout must be > 0 and retry count >= 0")
    if settings.fixed_tp_pct <= 0 or settings.fixed_sl_pct <= 0:
        raise ValueError("Fixed payout TP/SL price distances must be positive percentages")
    if settings.default_starting_balance <= 0 or settings.initial_margin_usdt <= 0:
        raise ValueError("DEFAULT_STARTING_BALANCE and INITIAL_MARGIN_USDT must be positive")
    if settings.initial_margin_usdt > settings.default_starting_balance:
        raise ValueError("INITIAL_MARGIN_USDT cannot exceed DEFAULT_STARTING_BALANCE")
    if settings.max_concurrent_positions != 1:
        raise ValueError("MAX_CONCURRENT_POSITIONS remains fixed at 1 for the separate paper-trading engine")
    if not 1 <= settings.backtest_max_positions <= 50:
        raise ValueError("BACKTEST_MAX_POSITIONS must be between 1 and 50")
    if not 0 < settings.backtest_max_open_risk_fraction <= 0.50:
        raise ValueError("BACKTEST_MAX_OPEN_RISK_FRACTION must be in (0, 0.50]")
    if not 1 <= settings.max_leverage <= 50:
        raise ValueError("MAX_LEVERAGE must be between 1 and 50")
    if settings.net_take_profit_usdt <= 0 or settings.requested_stop_loss_usdt <= 0:
        raise ValueError("NET_TAKE_PROFIT_USDT and REQUESTED_STOP_LOSS_USDT must be positive")
    if settings.backtest_margin_mode not in {"cross", "isolated"}:
        raise ValueError("BACKTEST_MARGIN_MODE must be cross or isolated")
    if settings.backtest_max_holding_minutes < 3:
        raise ValueError("BACKTEST_MAX_HOLDING_MINUTES must be at least 3")
    if settings.rsi_period < 2 or not 0 < settings.rsi_oversold < settings.rsi_overbought <= 100:
        raise ValueError("RSI settings require 0 < RSI_OVERSOLD < RSI_OVERBOUGHT <= 100")
    if not 2 <= settings.ema_fast < settings.ema_mid < settings.ema_slow:
        raise ValueError("EMA_FAST < EMA_MID < EMA_SLOW must hold and each period must be >=2")
    if settings.rsi_volume_period < 2 or settings.rsi_volume_multiplier <= 0:
        raise ValueError("Invalid RSI strategy volume settings")
    if settings.exchange_taker_fee_rate < 0 or settings.exchange_maker_fee_rate < 0:
        raise ValueError("Fee rates cannot be negative")
    if not 1 <= settings.mexc_universe_top_n <= 100:
        raise ValueError("MEXC_UNIVERSE_TOP_N must be between 1 and 100")
    if settings.signal_ttl_bars < 1 or not 0 <= settings.signal_min_score <= settings.signal_push_min_score <= 100:
        raise ValueError("Signal TTL/score thresholds are invalid")
    if settings.candidate_stop_atr_multiple <= 0 or settings.candidate_target_atr_multiple <= 0 or settings.candidate_min_net_rr <= 0:
        raise ValueError("Candidate ATR and net-RR settings must be positive")
    if not 50 <= settings.mtf_htf_warmup_bars <= 1000:
        raise ValueError("MTF_HTF_WARMUP_BARS must be between 50 and 1000")
    if not 0 < settings.live_max_spread_atr_fraction <= 1:
        raise ValueError("LIVE_MAX_SPREAD_ATR_FRACTION must be in (0, 1]")
    if settings.live_min_orderbook_depth_usdt < 0 or settings.max_adverse_funding_rate < 0:
        raise ValueError("Liquidity/funding gates cannot be negative")
    if not 50 <= settings.backtest_min_coverage_pct <= 100:
        raise ValueError("BACKTEST_MIN_COVERAGE_PCT must be between 50 and 100")
    if not 50 <= settings.backtest_min_universe_coverage_pct <= 100:
        raise ValueError("BACKTEST_MIN_UNIVERSE_COVERAGE_PCT must be between 50 and 100")
    if settings.app_env.lower() == "production" and not settings.database_url:
        raise ValueError("DATABASE_URL is required in production; local SQLite is not shared across Render services")
    return settings
