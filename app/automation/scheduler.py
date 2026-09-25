from __future__ import annotations

import asyncio
import logging
import time

from .scanner import MexcScanner

LOGGER = logging.getLogger(__name__)


class ScannerScheduler:
    """Run the MEXC scanner once per newly closed 5M candle."""

    def __init__(self, scanner: MexcScanner, interval_seconds: int = 300) -> None:
        self.scanner = scanner
        self.interval_seconds = max(60, int(interval_seconds))
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._scan_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task and not self._task.done(): return
        self._stopping.clear(); self._task = asyncio.create_task(self._run(), name="mexc-scanner-scheduler")
        LOGGER.info("MEXC scanner scheduler started (candle-driven 5M)")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel(); await asyncio.gather(self._task, return_exceptions=True); self._task = None
        LOGGER.info("MEXC scanner scheduler stopped")

    async def scan_now(self) -> None:
        if self._scan_lock.locked():
            LOGGER.warning("Skipping manual MEXC scan because a scan is already running")
            return
        async with self._scan_lock:
            started = time.monotonic()
            try:
                result = await self.scanner.scan_once()
                LOGGER.info("MEXC scanner cycle completed in %.2fs: %s", time.monotonic() - started, result)
            except Exception:
                LOGGER.exception("MEXC scanner cycle failed")

    async def _sleep_until_next_5m(self) -> None:
        now = time.time(); next_boundary = (int(now) // 300 + 1) * 300 + 2
        remaining = max(0.5, next_boundary - time.time())
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        await self.scan_now()
        while not self._stopping.is_set():
            await self._sleep_until_next_5m()
            if not self._stopping.is_set(): await self.scan_now()
