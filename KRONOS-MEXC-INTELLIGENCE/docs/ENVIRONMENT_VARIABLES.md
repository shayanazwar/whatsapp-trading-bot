# Environment variables

Copy `.env.example` to `.env` for local development. Production secrets belong in the Render environment dashboard, never in Git.

- `APP_ENV`: development or production.
- `APP_SECRET_KEY`: unique random secret, 32+ characters in production.
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`: unique dashboard credentials; password must be 14+ characters in production.
- `DATABASE_URL`: SQLite locally; Render PostgreSQL in production.
- `MEXC_API_BASE_URL`: `https://api.mexc.com`.
- `MEXC_HTTP_TIMEOUT`, `MEXC_MAX_RETRIES`, `MEXC_REQUEST_MIN_INTERVAL_SECONDS`: bounded public API behaviour and request pacing.
- `AUTO_SCAN_ENABLED`, `MARKET_SCAN_INTERVAL_SECONDS`, `UNIVERSE_LIMIT`: scheduled scanner behaviour.
- `MAX_SPREAD_BPS`, `MAX_BOOK_DISTANCE_BPS`, `MIN_BOOK_DEPTH_MULTIPLE`: fresh spread and MEXC visible-book capacity gates. Depth uses the documented third field of each level as contract quantity; it remains a snapshot estimate, not a fill guarantee.
- `MAX_SIGNAL_MARKET_DATA_AGE_SECONDS`, `MAX_SIGNAL_ENTRY_DRIFT_ATR`: fresh quote/order-book age and entry-drift rejection thresholds.
- `FORECAST_SAMPLE_COUNT`, `FORECAST_HORIZON`, `FORECAST_CHUNK_SIZE`, `FORECAST_TEMPERATURE`, `FORECAST_TOP_P`, `FORECAST_RANDOM_SEED`: Kronos sampling.
- `KRONOS_REPO_REVISION`, `KRONOS_MODEL_REVISION`, `KRONOS_TOKENIZER_REVISION`: pinned external model sources.
- `SIMULATION_REPLAYS`, `SIMULATION_BLOCK_LENGTH`: historical scenario generator.
- `BACKTEST_TAKER_FEE_RATE`, `BACKTEST_SLIPPAGE_BPS`, `MIN_NET_RR`: explicit cost assumptions and filters.
- `TP1_EXIT_FRACTION`, `TP2_R_MULTIPLE`: partial exit schedule. Defaults close 50% at 1R and the remaining 50% at 2.5R (1.75R weighted gross reward before costs).
- `RISK_PER_TRADE_PCT`, `MAX_PORTFOLIO_RISK_PCT`, `MAX_PORTFOLIO_NOTIONAL_MULTIPLE`, `DEFAULT_LEVERAGE`, `MARGIN_BUFFER_PCT`: paper sizing and portfolio caps. The fee override is a decimal rate (for example, `0.0005` for 0.05%); when omitted, the adapter uses the current contract metadata fee snapshot. Match this to the actual account/API execution fee schedule before interpreting results. The simulator currently models taker fills only.
- `PAPER_MODE=true`, `LIVE_TRADING_ENABLED=false`, `AUTOMATIC_ORDER_EXECUTION=false`: mandatory operating mode.
- Forecast paths are compressed and stored in PostgreSQL in this release. No S3/object-store backend is implemented yet.
- `WHATSAPP_ENABLED`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`: enable outbound Cloud API notifications.
- `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`: GET webhook verification and POST HMAC signature validation. `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`, `META_VERIFY_TOKEN` and `META_APP_SECRET` are accepted as credential aliases.
- `WHATSAPP_ALLOWED_RECIPIENTS`: comma-separated international phone numbers allowed to issue bot commands. Configure this before opening the webhook.
- `WHATSAPP_GRAPH_API_VERSION`, `WHATSAPP_API_TIMEOUT_SECONDS`, `WHATSAPP_API_MAX_RETRIES`: bounded API request settings.
- `PUBLIC_BASE_URL`: public HTTPS base URL used for chart image links. See `docs/WHATSAPP_SETUP.md`.
