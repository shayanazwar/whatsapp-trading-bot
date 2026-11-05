# Release Notes — v0.8.6

## Backtest execution on Render Free

- Fixed the queued-but-never-started path: the FastAPI web process now creates an inline S1-RFPC command processor and injects it into the durable queue scheduler.
- Started a queue-only consumer inside the existing web process at application startup. It polls independently of the optional market scanner and processes one backtest at a time.
- Kept the market scanner/paper scheduler separate from the backtest queue; the Render Blueprint enables it in the same web process.
- Removed the separate Render background worker from the Blueprint. One web instance with one Uvicorn worker is the intended topology.
- Reused the PostgreSQL advisory lock from the legacy standalone worker to refuse duplicate consumers during migration.
- Added queue heartbeat/status fields to `/healthz`.
- Interrupted in-progress jobs are returned to `queued` during graceful shutdown, preserving the job ID and args.
- Updated the user acknowledgement to state that the same Render service processes the job.
- Live execution remains disabled.

## Verification

- `pytest -q`: 180 passed.
- `python -m compileall -q app`: passed.
- Added end-to-end regression for `BACKTEST` enqueue → same-process execution → persisted completion.
