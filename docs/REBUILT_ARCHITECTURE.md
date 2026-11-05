# Current Rebuilt Architecture — v0.8.1

This document supersedes older v0.4/v0.7 architecture notes. Current runtime topology, job lifecycle, worker health and deployment checks are maintained in [`ARCHITECTURE_AND_OPERATIONS.md`](ARCHITECTURE_AND_OPERATIONS.md).

The active strategy path is S1-RFPC using completed 1D → 12H → 4H → 1H candles. The WhatsApp/web process persists a backtest job to PostgreSQL; a separate Render background worker claims and runs it. The worker's queue poll interval is independently configurable through `BACKTEST_JOB_POLL_SECONDS` (default 5 seconds). The same PostgreSQL database is mandatory for both services.

The earlier 3M RSI/EMA research implementation remains in the repository for historical comparison only. It is not dispatched by the `BACKTEST` or `SCAN` commands. The active backtest and scanner are S1-RFPC.

No token fundamentals provider is integrated in this release. Current funding, ticker, OI and order-book data are derivatives/market metrics, not tokenomics or on-chain fundamentals. See [`FUNDAMENTALS_DATA_AUDIT.md`](FUNDAMENTALS_DATA_AUDIT.md).
