from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

import websockets

from app.config import get_settings

log = logging.getLogger(__name__)


class MexcTickerFeed:
    """Official JSON ticker WebSocket feed with independent heartbeat and reconnect."""

    def __init__(self, on_tickers: Callable[[list[dict]], Awaitable[None]], *,
                 ping_interval_seconds: float = 15.0, pong_timeout_seconds: float = 35.0):
        if not 10 <= ping_interval_seconds <= 20:
            raise ValueError("MEXC WebSocket ping interval must be between 10 and 20 seconds")
        if pong_timeout_seconds <= ping_interval_seconds:
            raise ValueError("pong timeout must exceed ping interval")
        self.url = get_settings().mexc_ws_url
        self.on_tickers = on_tickers
        self.ping_interval_seconds = ping_interval_seconds
        self.pong_timeout_seconds = pong_timeout_seconds
        self.running = False

    async def _ping_loop(self, ws):
        """Send heartbeats independently of ticker traffic, as MEXC requires."""
        while self.running:
            await asyncio.sleep(self.ping_interval_seconds)
            if self.running:
                await ws.send(json.dumps({"method": "ping"}))

    async def _dispatch(self, payload: dict) -> bool:
        channel = payload.get("channel")
        if channel == "push.tickers" and isinstance(payload.get("data"), list):
            await self.on_tickers(payload["data"])
        elif channel == "rs.error":
            raise ConnectionError(f"MEXC WebSocket reported an error: {payload}")
        return channel == "pong"

    async def run(self):
        self.running = True
        delay = 1.0
        while self.running:
            ping_task = None
            try:
                async with websockets.connect(self.url, ping_interval=None, close_timeout=5) as ws:
                    await ws.send(json.dumps({"method": "sub.tickers", "param": {}, "gzip": False}))
                    delay = 1.0
                    last_pong = time.monotonic()
                    ping_task = asyncio.create_task(self._ping_loop(ws), name="mexc-ws-heartbeat")
                    while self.running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=5)
                        except TimeoutError:
                            if time.monotonic() - last_pong > self.pong_timeout_seconds:
                                raise TimeoutError("MEXC WebSocket pong heartbeat timed out")
                            continue
                        if isinstance(message, bytes):
                            message = message.decode("utf-8", errors="strict")
                        payload = json.loads(message)
                        if not isinstance(payload, dict):
                            raise ValueError("Unexpected MEXC WebSocket message shape")
                        if await self._dispatch(payload):
                            last_pong = time.monotonic()
                        if time.monotonic() - last_pong > self.pong_timeout_seconds:
                            raise TimeoutError("MEXC WebSocket pong heartbeat timed out")
                        if ping_task.done():
                            error = ping_task.exception()
                            if error is not None:
                                raise ConnectionError(f"MEXC WebSocket ping task failed: {error}") from error
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # bounded exponential reconnect
                if self.running:
                    log.warning("MEXC ticker WebSocket disconnected: %s", exc)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
        log.info("MEXC ticker WebSocket feed stopped")

    def stop(self):
        self.running = False
