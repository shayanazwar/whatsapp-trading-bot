from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from app.config import get_settings

log = logging.getLogger(__name__)


class MexcTickerFeed:
    """Official JSON ticker WebSocket feed with reconnect and heartbeat handling."""
    def __init__(self, on_tickers: Callable[[list[dict]], Awaitable[None]]):
        self.url = get_settings().mexc_ws_url
        self.on_tickers = on_tickers
        self.running = False

    async def run(self):
        self.running = True
        delay = 1.0
        while self.running:
            try:
                async with websockets.connect(self.url, ping_interval=None, close_timeout=5) as ws:
                    await ws.send(json.dumps({"method": "sub.tickers", "param": {}, "gzip": False}))
                    delay = 1.0
                    while self.running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=25)
                        except TimeoutError:
                            await ws.send(json.dumps({"method": "ping"}))
                            continue
                        if isinstance(message, bytes):
                            message = message.decode("utf-8", errors="replace")
                        payload = json.loads(message)
                        channel = payload.get("channel")
                        if channel == "push.tickers" and isinstance(payload.get("data"), list):
                            await self.on_tickers(payload["data"])
                        elif channel in {"pong", "rs.error"}:
                            log.debug("MEXC WS control message: %s", channel)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # bounded exponential reconnect
                log.warning("MEXC ticker WebSocket disconnected: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def stop(self):
        self.running = False
