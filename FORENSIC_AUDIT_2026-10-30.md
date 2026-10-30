# Forensic Audit — MEXC Top-100 3M Backtest and Scanner

**Build:** 0.5.0  
**Audit date:** 2026-10-30  
**Scope:** Uploaded `whatsapp-trading-bot-main (38).zip`, the reported 1D/7D behavior, Render memory-limit notification, and dedicated worker scanner.

## Executive findings

Two independent defects explain the reported behavior in the uploaded source:

1. **The scanner was disabled by default.** `MEXC_UNIVERSE_SCAN_ENABLED` defaulted to `false`, and the uploaded Render Blueprint did not override it. The worker could start normally while skipping the top-universe scan on every scheduler cycle. Because per-symbol data errors were not persisted, an operator could not distinguish "no signals" from "scan failed for these symbols".
2. **The 7D backtest could exceed web-process memory.** The old path loaded 100 symbols with 8 concurrent 1M→3M aggregations and then loaded Fair Price/funding support for many symbols, retaining more intermediate data than necessary. The command executes inside the web service process; when the process is killed for exceeding its memory limit, it cannot finish the report or send a WhatsApp reply.

The Render notification is therefore **not an email-delivery failure**. It is Render's infrastructure email reporting that the web service was restarted after exceeding its memory limit.

## Evidence in the uploaded source

- `app/config.py`: `MEXC_UNIVERSE_SCAN_ENABLED` defaults to `false`.
- Original `render.yaml`: no `MEXC_UNIVERSE_SCAN_ENABLED=true` override was present.
- `app/services/scheduler.py`: the top-100 scan is conditional on this setting. When false, the worker does not call `_universe_scan_tick()`.
- Original `app/services/command_processor.py`: the top-100 history path used higher concurrency and retained additional Fair Price/funding history, increasing peak memory for multi-day backtests.
- The uploaded repository has no `.git` metadata and its README contains a placeholder GitHub remote. The connected GitHub integration exposed no repositories during this audit. Consequently, the exact private GitHub branch/commit that Render currently deploys could not be verified.
- There is a source/deployment identity mismatch: the uploaded V38 project identifies as v0.4.0 and the old backtest engine contains an `isolated_margin=True` setting, while the user's live WhatsApp report shows LONG and SHORT signals and `Margin: CROSS`. That live output did not originate from the exact unmodified V38 source archive.

## Implemented fixes in build 0.5.0

### 1. Memory-bounded backtest history loader

- Limit concurrent full-history 1M→3M market-data loads to **2 symbols**.
- Consume each completed symbol task as it finishes rather than waiting for one `gather()` of all 100 histories.
- Reduce warm-up from 450 to 300 completed 3M bars (`max(300, 3 × slow EMA period)`). Warm-up bars are kept outside the requested test window and are not counted as test-period candles.
- Discard raw 1M source bars once that symbol's 3M candles have been aggregated.
- Run an initial simulation pass to identify symbols that actually receive a position. Load Fair Price/funding support data only for those symbols, sequentially, rather than for every raw signal symbol.
- Aggregate Fair Price to 3M and discard raw Fair Price 1M candles after aggregation.
- If a liquidation changes which later trade is entered, replay the simulator and load support data for newly entered symbols. The replay is bounded; if the pass limit is reached, the report adds an explicit incomplete-support warning.
- Add per-symbol data-load errors and progress logging, including process RSS, so a long run can be located in Render logs.

### 2. Scanner enablement and diagnostics

- Render Blueprint sets `MEXC_UNIVERSE_TOP_N=100` and `MEXC_UNIVERSE_SCAN_ENABLED=true` on both services through the shared environment block. Only the dedicated worker runs the scheduler because the web service retains `SCHEDULER_ENABLED=false`.
- The worker logs its build ID, top-N universe size, and auto-scan setting at startup.
- The worker persists a 20-second heartbeat. `/healthz` exposes build version, scanner enabled state, top-N, worker status, heartbeat, last attempt/success, last summary, and last error.
- Every selected symbol receives a persisted `SIGNAL`, `NO_SIGNAL`, or `DATA_ERROR` outcome. Data failures are no longer silently skipped.
- Scan start/completion and per-symbol errors are logged for operations review.

### 3. Build identity and failure behavior

- Build version is now `0.5.0`; FastAPI metadata, `/healthz`, and worker startup log expose this identity so the deployed service can be checked against the patch.
- An unexpected command exception is logged with its traceback and returns a clear "no report generated" failure message rather than silently suggesting partial output.

## How to verify after deployment

1. Deploy this exact patch to the same GitHub branch connected to both Render services, or apply the runtime ZIP to the correct repository before deploying.
2. Ensure the **Worker service** has `MEXC_UNIVERSE_SCAN_ENABLED=true`, `MEXC_UNIVERSE_TOP_N=100`, and the same `DATABASE_URL` as the web service. If using the Blueprint, sync/deploy the Blueprint so the worker environment receives the values.
3. Open `/healthz` on the web URL. Expected fields include:
   - `build_version`: `0.5.0`
   - `scanner_enabled`: `true`
   - `scanner_top_n`: `100`
   - `scanner_worker_status`: `running`
   - `scanner_worker_heartbeat_utc`: a recent timestamp
   - `scanner_last_success`: a recent timestamp after the first complete scan
   - `scanner_last_summary`: selected/scanned/signal/error counts
4. In Worker logs, look for `MEXC research worker starting ... build=0.5.0 ... auto_scan=True`, then `TOP-100 3M SCAN START` and `TOP-100 3M SCAN COMPLETE`.
5. Run `BACKTEST 7D`. Its `Requested UTC Window` must span approximately seven days. The report must show actual data coverage and per-symbol failures. With 100 complete symbols, seven days contains up to 3,360 completed 3M candles per symbol; with 300 warm-up bars, the in-memory set is approximately 366,000 3M candles, subject to data gaps and exchange availability.
6. Confirm the log entry `BACKTEST 3M COMPLETE` has `requested_window=7D`, `universe=100`, `loaded=...`, `candles=...`, `elapsed_s=...`, and `rss_mb=...`.

## Verification performed in this workspace

- Python compilation: PASS.
- Automated test suite: **108 passed**.
- Render Blueprint YAML parse: PASS; the scanner is enabled in the Blueprint and the web in-process scheduler remains disabled.
- Synthetic memory fixture: 100 symbols × 3,660 3M candles = 366,000 candles; local simulation peak RSS measured approximately **217.2 MB** in this container. This is a controlled local fixture, not a measurement of Render or live MEXC downloads.
- ZIP integrity and patch application: checked during packaging.

## Verification not performed

- No actual live 100-symbol MEXC 7D download was run in this environment.
- No Render deployment or Render memory graph was accessible, so the production memory ceiling and recovery are not verified.
- No private GitHub repository/branch/commit was accessible through the connected GitHub integration, and the uploaded archive contained no Git remote. The deployed branch must be confirmed by the operator.
- Synthetic OHLC fills remain a model. Historical exchange candles are used, but exact order-book impact, queue position, partial fills, intrabar path, external account positions, and external open-order margin are not reconstructed. This is not a promise of real exchange execution or profit.

## Operational note about Render's email

The Render email is the alert for the web process exceeding its memory limit and being restarted. The message itself shows that Render's email notification path worked. This patch reduces the identified memory drivers, but it cannot certify recovery until the new build is deployed and a 7D run completes on the actual Render instance. If the new version still exceeds the configured memory limit, the next architectural step is to move backtests to a separate bounded worker/job queue rather than run them inside the web service process.
