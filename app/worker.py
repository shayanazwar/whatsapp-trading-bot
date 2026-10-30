"""Standalone worker for the top-100 MEXC 3-minute RSI/EMA research scanner.

The worker runs ResearchScheduler, which can scan active USDT perpetuals once
per completed 3-minute slot. Legacy paper-position processing is separate and
remains disabled by default. This worker cannot submit live orders.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Any

from app.config import load_settings
from app.version import APP_VERSION
from app.mexc.client import MexcClient
from app.services.scheduler import ResearchScheduler
from app.storage import Store
from app.whatsapp import WhatsAppCloud

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Keep application-stage and warning/error logs, but suppress one INFO line per
# successful MEXC/Meta HTTP request. Backtest progress is logged by app services.
for _quiet_logger in ("httpx", "httpcore"):
    logging.getLogger(_quiet_logger).setLevel(logging.WARNING)
logger = logging.getLogger("mexc-swing-research-worker")


class SingleWorkerLock:
    """PostgreSQL advisory lock held for the lifetime of the scheduler worker."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.connection: Any = None

    def acquire(self) -> bool:
        if not self.database_url:
            return True  # Local development assumes a single manually started worker.
        try:
            import psycopg
            self.connection = psycopg.connect(
                self.database_url.replace("postgresql+psycopg://", "postgresql://", 1),
                connect_timeout=10,
                autocommit=True,
            )
            row = self.connection.execute("SELECT pg_try_advisory_lock(834752019)").fetchone()
            if not row or not bool(row[0]):
                self.connection.close()
                self.connection = None
                return False
            return True
        except Exception:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            raise

    def release(self) -> None:
        if self.connection is not None:
            try:
                self.connection.execute("SELECT pg_advisory_unlock(834752019)")
            finally:
                self.connection.close()
                self.connection = None


def _active_contract_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Retained metadata-validation helper for legacy unit tests and diagnostics."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or row.get("contractCode") or "").upper()
        if not symbol:
            continue
        try:
            if row.get("state") is None or int(row["state"]) != 0:
                continue
            contract_size = float(row.get("contractSize", 0))
            vol_unit = float(row.get("volUnit", 0))
            min_vol = float(row.get("minVol", 0))
            max_vol = float(row.get("maxVol", 0))
            max_leverage = max(
                float(row.get("countryConfigContractMaxLeverage", 0)),
                float(row.get("maxLeverage", 0)),
            )
            if min(contract_size, vol_unit, min_vol, max_vol, max_leverage) <= 0:
                continue
            if min_vol > max_vol:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        out[symbol] = dict(row)
    return out


async def _heartbeat(store: Store, stop: asyncio.Event) -> None:
    """Persist a heartbeat so the web health endpoint can detect a stale worker."""
    while not stop.is_set():
        store.set_state("worker_heartbeat_utc", datetime.now(timezone.utc).isoformat())
        try:
            await asyncio.wait_for(stop.wait(), timeout=20.0)
        except TimeoutError:
            continue


async def _run() -> None:
    settings = load_settings()
    store = Store(settings.database_url or settings.database_path)
    lock = SingleWorkerLock(settings.database_url)
    if not lock.acquire():
        raise RuntimeError(
            "Another research worker holds the database advisory lock; refusing duplicate workers"
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    mexc: MexcClient | None = None
    whatsapp: WhatsAppCloud | None = None
    scheduler: ResearchScheduler | None = None
    scheduler_task: asyncio.Task | None = None
    stop_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None
    try:
        mexc = MexcClient(
            base_url=settings.mexc_rest_base_url,
            timeout_seconds=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
        )
        whatsapp = WhatsAppCloud(settings)
        scheduler = ResearchScheduler(settings, store, mexc, whatsapp)
        store.set_state("worker_status", "starting")
        logger.info(
            "MEXC research worker starting | build=%s | universe=top-%d | strategy=3M-RSI-EMA | auto_scan=%s | paper=%s | live_execution=false",
            APP_VERSION,
            settings.mexc_universe_top_n,
            settings.mexc_universe_scan_enabled,
            settings.paper_trading_enabled,
        )
        # This is a dedicated Render worker. SCHEDULER_ENABLED controls only the web
        # process's optional in-process scheduler; it must not disable this worker.
        scheduler_task = asyncio.create_task(
            scheduler.run_forever(), name="swing-research-scheduler"
        )
        store.set_state("worker_status", "running")
        heartbeat_task = asyncio.create_task(_heartbeat(store, stop), name="worker-heartbeat")
        stop_task = asyncio.create_task(stop.wait(), name="swing-research-shutdown-waiter")
        done, _ = await asyncio.wait(
            {scheduler_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if scheduler_task in done and not stop.is_set():
            # A scheduler that exits on its own is a service failure, not a healthy idle worker.
            await scheduler_task
            raise RuntimeError("ResearchScheduler exited unexpectedly")
    finally:
        if scheduler is not None:
            scheduler.stop()
        stop.set()
        if heartbeat_task is not None:
            try:
                await asyncio.wait_for(heartbeat_task, timeout=2.0)
            except TimeoutError:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
        if stop_task is not None:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        if scheduler_task is not None:
            try:
                await asyncio.wait_for(
                    scheduler_task,
                    timeout=max(30, settings.scheduler_interval_seconds + 15),
                )
            except TimeoutError:
                scheduler_task.cancel()
                await asyncio.gather(scheduler_task, return_exceptions=True)
            except Exception as exc:
                logger.error("Swing research scheduler exited with error (%s)", type(exc).__name__)
        if mexc is not None:
            await mexc.aclose()
        if whatsapp is not None:
            await whatsapp.aclose()
        store.set_state("worker_status", "stopped")
        lock.release()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
