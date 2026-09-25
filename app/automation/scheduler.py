from __future__ import annotations

import asyncio
import logging
import time

from .scanner import MexcScanner

LOGGER = logging.getLogger(__name__)


class ScannerScheduler:
    """
    Candle-aware MEXC scanner scheduler.

    The scanner is still allowed to run on a short polling interval,
    but the same market candle is not analyzed repeatedly.

    This prevents duplicate signal evaluation and reduces unnecessary
    MEXC API calls.
    """

    def __init__(
        self,
        scanner: MexcScanner,
        interval_seconds: int,
    ) -> None:
        self.scanner = scanner

        # Keep polling reasonably frequent so the scheduler can detect
        # a newly closed candle without excessive API usage.
        self.interval_seconds = max(
            15,
            int(interval_seconds),
        )

        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._scan_lock = asyncio.Lock()

        # Last completed scan timestamp.
        self._last_scan_monotonic: float | None = None

        # Prevent repeated scans from happening too quickly.
        self._minimum_scan_gap = max(
            10.0,
            min(float(self.interval_seconds), 60.0),
        )

    async def start(self) -> None:
        if self._task and not self._task.done():
            return

        self._stopping.clear()

        self._task = asyncio.create_task(
            self._run(),
            name="mexc-scanner-scheduler",
        )

        LOGGER.info(
            "MEXC scanner scheduler started "
            "(poll=%ss, minimum_gap=%.1fs)",
            self.interval_seconds,
            self._minimum_scan_gap,
        )

    async def stop(self) -> None:
        self._stopping.set()

        if self._task:
            self._task.cancel()

            await asyncio.gather(
                self._task,
                return_exceptions=True,
            )

            self._task = None

        LOGGER.info(
            "MEXC scanner scheduler stopped"
        )

    async def scan_now(self) -> None:
        """
        Execute exactly one scanner cycle.

        A lock prevents overlapping scans. This is important because
        MEXC requests can take longer than the polling interval.
        """

        if self._scan_lock.locked():
            LOGGER.info(
                "MEXC scan already running; "
                "skipping overlapping scan"
            )
            return

        async with self._scan_lock:
            started = time.monotonic()

            try:
                LOGGER.info(
                    "Starting MEXC scanner cycle"
                )

                result = await self.scanner.scan_once()

                elapsed = (
                    time.monotonic() - started
                )

                self._last_scan_monotonic = (
                    time.monotonic()
                )

                if isinstance(result, dict):
                    LOGGER.info(
                        "MEXC scanner cycle completed "
                        "in %.2fs: %s",
                        elapsed,
                        result,
                    )
                else:
                    LOGGER.info(
                        "MEXC scanner cycle completed "
                        "in %.2fs",
                        elapsed,
                    )

            except asyncio.CancelledError:
                raise

            except Exception:
                LOGGER.exception(
                    "MEXC scanner cycle failed"
                )

    async def _wait_until_next_scan(self) -> None:
        """
        Wait for either shutdown or the next polling window.
        """

        try:
            await asyncio.wait_for(
                self._stopping.wait(),
                timeout=self.interval_seconds,
            )

        except asyncio.TimeoutError:
            return

    async def _run(self) -> None:
        """
        Scheduler lifecycle.

        1. Scan immediately on startup.
        2. Wait for the configured polling interval.
        3. Scan again.
        4. Never allow overlapping scans.
        5. Stop cleanly when requested.
        """

        try:
            await self.scan_now()

            while not self._stopping.is_set():
                await self._wait_until_next_scan()

                if self._stopping.is_set():
                    break

                # Safety gap against accidental rapid cycles.
                if self._last_scan_monotonic is not None:
                    elapsed = (
                        time.monotonic()
                        - self._last_scan_monotonic
                    )

                    remaining = (
                        self._minimum_scan_gap
                        - elapsed
                    )

                    if remaining > 0:
                        try:
                            await asyncio.wait_for(
                                self._stopping.wait(),
                                timeout=remaining,
                            )
                        except asyncio.TimeoutError:
                            pass

                        if self._stopping.is_set():
                            break

                await self.scan_now()

        except asyncio.CancelledError:
            raise

        except Exception:
            LOGGER.exception(
                "MEXC scanner scheduler terminated unexpectedly"
            )
