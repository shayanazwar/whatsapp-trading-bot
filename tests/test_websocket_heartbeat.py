import asyncio
from unittest.mock import AsyncMock

import pytest

from app.market.websocket import MexcTickerFeed


@pytest.mark.asyncio
async def test_websocket_heartbeat_sends_independently_of_ticker_messages():
    # Exercise the timer loop at sub-second test speed without weakening the
    # production constructor's exchange-required 10-20 second validation.
    feed = object.__new__(MexcTickerFeed)
    feed.on_tickers = AsyncMock()
    feed.ping_interval_seconds = 0.01
    websocket = AsyncMock()
    feed.running = True
    task = asyncio.create_task(feed._ping_loop(websocket))
    try:
        await asyncio.sleep(0.035)
    finally:
        feed.running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert websocket.send.await_count >= 2
    assert '"method": "ping"' in websocket.send.await_args.args[0]


def test_websocket_ping_interval_must_match_exchange_requirement():
    with pytest.raises(ValueError, match="between 10 and 20 seconds"):
        MexcTickerFeed(AsyncMock(), ping_interval_seconds=25)
