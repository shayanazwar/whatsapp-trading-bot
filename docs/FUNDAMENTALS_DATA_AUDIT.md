# Fundamentals Data Audit — v0.8.1

## Verdict

Token-fundamentals analysis is **not implemented** in this release. The `ANALYZE` command is a completed-candle technical/structure analysis using S1-RFPC. It must not be described or marketed as a complete fundamental analysis.

The code now returns `fundamentals.status = NOT_IMPLEMENTED`, `provider = null`, an empty `data` object, and a user-visible explanation. Missing fundamental data is not replaced with fabricated values and no fundamental score is calculated.

## What exists today

- OHLCV/candle data and derived EMA, RSI and ATR metrics.
- S1-RFPC regime, structure, setup and 1H trigger evaluation.
- Current funding-rate snapshots and historical funding settlements, used as derivatives carry/cost context.
- Current contract metadata, ticker snapshots and order-book depth used for market-quality/execution research.

These are technical, derivatives or market-microstructure inputs. Funding rates, open interest, turnover and order-book depth are not substitutes for token fundamentals. In particular, current ticker/OI data must not be backfilled into historical bars as though it were historically observed.

## Missing fundamental datasets

This release does not have a verified provider/adapter for: market capitalization; circulating, total or maximum supply; FDV; vesting/unlock schedules; holder concentration; on-chain transactions/active addresses; protocol fees/revenue/TVL; developer activity; governance; audited token allocations; or timestamped project/news events.

No fundamentals-based trade filter or ranking is enabled. The strategy is technical/price-structure research with derivatives market-quality checks.

## Requirements before adding fundamentals

1. Select an authorized, documented provider and obtain credentials/permission where required.
2. Build a typed adapter with symbol/project mapping, source timestamps, units, quote currency and provenance.
3. Persist timestamped observations so historical backtests use only data that was available at each simulated decision time.
4. Add freshness/coverage/conflict checks; missing or stale metrics must remain `UNKNOWN`, not zero.
5. Separate token/project fundamentals from futures-market features (funding, open interest, basis and order book).
6. Add deterministic unit tests, provider contract tests using fixtures, and look-ahead-bias tests before allowing fundamentals to influence signals.

## External endpoint notes

The public MEXC Futures contract detail, ticker, kline and funding endpoints expose contract/market data. They are not a complete tokenomics or on-chain fundamentals provider. See the official MEXC Futures API documentation: https://www.mexc.com/api-docs/futures/
