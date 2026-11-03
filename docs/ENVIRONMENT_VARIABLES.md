# Environment variables

Copy `.env.example` to `.env` for local development. Production secrets belong in the Render environment dashboard, never in Git.

- `APP_ENV`: development or production.
- `APP_SECRET_KEY`: unique random secret, 32+ characters in production.
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`: unique dashboard credentials; password must be 14+ characters in production.
- `DATABASE_URL`: SQLite locally; Render PostgreSQL in production.
- `MEXC_API_BASE_URL`: `https://api.mexc.com`.
- `MEXC_HTTP_TIMEOUT`, `MEXC_MAX_RETRIES`, `MEXC_REQUEST_MIN_INTERVAL_SECONDS`: bounded public API behaviour and request pacing.
- `AUTO_SCAN_ENABLED`, `MARKET_SCAN_INTERVAL_SECONDS`, `UNIVERSE_LIMIT`: scheduled scanner behaviour.
- `FORECAST_SAMPLE_COUNT`, `FORECAST_HORIZON`, `FORECAST_CHUNK_SIZE`, `FORECAST_TEMPERATURE`, `FORECAST_TOP_P`, `FORECAST_RANDOM_SEED`: Kronos sampling.
- `KRONOS_REPO_REVISION`, `KRONOS_MODEL_REVISION`, `KRONOS_TOKENIZER_REVISION`: pinned external model sources.
- `SIMULATION_REPLAYS`, `SIMULATION_BLOCK_LENGTH`: historical scenario generator.
- `BACKTEST_TAKER_FEE_RATE`, `BACKTEST_SLIPPAGE_BPS`, `MIN_NET_RR`: explicit cost assumptions and filters.
- `PAPER_MODE=true`, `LIVE_TRADING_ENABLED=false`, `AUTOMATIC_ORDER_EXECUTION=false`: mandatory operating mode.
- Forecast paths are compressed and stored in PostgreSQL in this release. No S3/object-store backend is implemented yet.
- No WhatsApp API credentials are collected. Crypto-futures signal delivery is blocked by the current Meta policy; dashboard signals are the supported channel.
