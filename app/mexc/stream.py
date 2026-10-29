"""Minimal MEXC public Futures ticker stream with bounded reconnects."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, AsyncIterator

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

logger = logging.getLogger(__name__)
MEXC_FUTURES_WS_URL = "wss://contract.mexc.com/edge"


async def ticker_updates(stop_event: asyncio.Event | None = None) -> AsyncIterator[dict[str, Any]]:
    """Yield ticker dictionaries from the official public all-contract ticker stream.

    If the stream cannot be kept healthy this generator reconnects with bounded exponential
    backoff; callers should fail closed when no recent ticker updates are available.
    """
    delay = 1.0
    while stop_event is None or not stop_event.is_set():
        try:
            async with connect(
                MEXC_FUTURES_WS_URL, open_timeout=10, close_timeout=5,
                ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024,
            ) as ws:
                await ws.send(json.dumps({"method": "sub.tickers", "param": {}}))
                logger.info("MEXC Futures all-ticker WebSocket connected")
                delay = 1.0
                while stop_event is None or not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except TimeoutError:
                        await ws.send(json.dumps({"method": "ping"}))
                        continue
                    if not isinstance(raw, (str, bytes)):
                        continue
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(msg, dict):
                        continue
                    channel = str(msg.get("channel", ""))
                    if channel not in {"push.tickers", "push.ticker"}:
                        continue
                    data = msg.get("data")
                    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
                    for row in rows:
                        if isinstance(row, dict) and row.get("symbol"):
                            yield row
        except asyncio.CancelledError:
            raise
        except (OSError, TimeoutError, ConnectionClosed, WebSocketException, ValueError) as exc:
            logger.warning("MEXC ticker stream disconnected (%s); reconnecting", type(exc).__name__)
            if stop_event is not None and stop_event.is_set():
                break
            await asyncio.sleep(delay + random.uniform(0, min(0.25, delay / 4)))
            delay = min(30.0, delay * 2)
