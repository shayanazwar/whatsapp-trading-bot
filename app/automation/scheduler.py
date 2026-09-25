from __future__ import annotations

import asyncio
import logging

from .scanner import MexcScanner

LOGGER = logging.getLogger(__name__)


class ScannerScheduler:
    def __init__(self, scanner: MexcScanner, interval_seconds: int) -> None:
        self.scanner = scanner
        self.interval_seconds = max(15, interval_seconds)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._scan_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="mexc-scanner-scheduler")
        LOGGER.info("MEXC scanner scheduler started (interval=%ss)", self.interval_seconds)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        LOGGER.info("MEXC scanner scheduler stopped")

    async def scan_now(self) -> None:
        if self._scan_lock.locked():
            LOGGER.info("MEXC scan already running; skipping overlapping scan")
            return
        async with self._scan_lock:
            try:
                await self.scanner.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("MEXC scanner cycle failed")

    async def _run(self) -> None:
        # Scan once immediately on startup, then on the fixed interval.
        await self.scan_now()
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                await self.scan_now()
