from __future__ import annotations

import asyncio
import logging
import time

from .scanner import MexcScanner

LOGGER = logging.getLogger(__name__)


class ScannerScheduler:
    """Run the MEXC scanner on closed-15M boundaries without overlap."""

    def __init__(
        self,
        scanner: MexcScanner,
        interval_seconds: int = 900,
        alignment_seconds: int = 900,
    ) -> None:
        self.scanner = scanner
        self.interval_seconds = max(900, int(interval_seconds))
        # The signal engine is driven by closed 15M structure. Running more
        # frequently cannot create new 15M information and only adds API/CPU load.
        self.alignment_seconds = max(900, int(alignment_seconds), self.interval_seconds)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._scan_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return

        self._stopping.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="mexc-scanner-scheduler",
        )

        LOGGER.info(
            "MEXC scanner scheduler started (interval=%ss alignment=%ss)",
            self.interval_seconds,
            self.alignment_seconds,
        )

    async def stop(self) -> None:
        self._stopping.set()

        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

        LOGGER.info("MEXC scanner scheduler stopped")

    async def scan_now(self) -> None:
        if self._scan_lock.locked():
            LOGGER.warning(
                "Skipping manual MEXC scan because a scan is already running"
            )
            return

        async with self._scan_lock:
            started = time.monotonic()

            try:
                result = await self.scanner.scan_once()

                LOGGER.info(
                    "MEXC scanner cycle completed in %.2fs: %s",
                    time.monotonic() - started,
                    result,
                )

            except Exception:
                LOGGER.exception("MEXC scanner cycle failed")

    async def _sleep_until_next_boundary(self) -> None:
        now = int(time.time())
        boundary = ((now // self.alignment_seconds) + 1) * self.alignment_seconds
        # A small grace period lets exchanges publish the newly closed candle.
        delay = max(0.0, boundary + 3 - time.time())
        try:
            await asyncio.wait_for(
                self._stopping.wait(),
                timeout=delay,
            )
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        # Run one scan immediately at startup, then align to closed 15M bars.
        await self.scan_now()

        while not self._stopping.is_set():
            await self._sleep_until_next_boundary()
            if not self._stopping.is_set():
                await self.scan_now()
