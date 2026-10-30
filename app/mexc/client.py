"""Public-only MEXC Futures REST client.

No authenticated trading endpoints are implemented here. GET retries are bounded; a future
signed order POST must never be retried blindly after an ambiguous timeout.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from collections import defaultdict
from typing import Any, Mapping

import httpx

from app.market.candles import (Candle, INTERVAL_SECONDS, aggregate_3d_from_1d, aggregate_3m_from_1m, aggregate_12h_from_4h, parse_kline_payload)
from app.symbols import normalize_symbol

logger = logging.getLogger(__name__)


class MexcError(RuntimeError):
    pass


class MexcRequestError(MexcError):
    pass


class MexcRateLimitError(MexcRequestError):
    pass


class MexcClient:
    """A conservative async client for unauthenticated MEXC Futures market endpoints."""

    def __init__(
        self,
        base_url: str = "https://api.mexc.com",
        timeout_seconds: float = 12.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
        min_interval_by_family: Mapping[str, float] | None = None,
    ) -> None:
        parsed_base = urlparse(base_url)
        if parsed_base.scheme != "https" or parsed_base.hostname != "api.mexc.com" or parsed_base.port not in (None, 443):
            raise ValueError("REST requests must use the exact official Futures host https://api.mexc.com")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._client = client
        self._owns_client = client is None
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = defaultdict(float)
        self._last_server_time: int | None = None
        self.server_time_source: str | None = None
        # The contract-detail endpoint returns the full market list. A short cache prevents
        # one identical full-list request per symbol during a multi-symbol backtest.
        self._contract_details_cache: tuple[float, tuple[dict[str, Any], ...]] | None = None
        self._contract_details_cache_ttl = 60.0
        self._contract_details_cache_lock = asyncio.Lock()
        # Klines: published 20 req/2s -> max theoretical 0.1s spacing; operate below that.
        # Other families are intentionally throttled separately and conservatively.
        self._min_interval = {
            "kline": 0.11,
            "ticker": 0.12,
            "contract": 0.25,
            "funding": 0.25,
            "server_time": 0.25,
            "depth": 0.25,
            "trades": 0.25,
        }
        if min_interval_by_family:
            self._min_interval.update(min_interval_by_family)

    async def __aenter__(self) -> "MexcClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                headers={"User-Agent": "MEXC-Futures-Research-Bot/0.3.0", "Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _throttle(self, family: str) -> None:
        async with self._locks[family]:
            interval = self._min_interval.get(family, 0.25)
            now = time.monotonic()
            wait = interval - (now - self._last_request[family])
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[family] = time.monotonic()

    @staticmethod
    def _response_ok(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return True
        if payload.get("success") is False:
            return False
        code = payload.get("code")
        if code not in (None, 0, "0", 200, "200") and payload.get("success") is not True:
            return False
        return True

    @staticmethod
    def _retry_after_seconds(value: str | None, attempt: int) -> float:
        """Parse both Retry-After delta-seconds and HTTP-date forms safely."""
        fallback = min(1.0 * (2**attempt), 8.0)
        if not value:
            return fallback
        try:
            seconds = float(value)
            if not math.isfinite(seconds):
                return fallback
        except (TypeError, ValueError, OverflowError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
                if not math.isfinite(seconds):
                    return fallback
            except (TypeError, ValueError, OverflowError, IndexError):
                return fallback
        return min(max(seconds, 0.0), 30.0)

    async def _get(self, path: str, *, params: Mapping[str, Any] | None = None, family: str = "other") -> Any:
        client = await self._ensure_client()
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._throttle(family)
            try:
                response = await client.get(url, params=params)
                if response.status_code == 429 or response.status_code == 418:
                    retry_after = response.headers.get("Retry-After")
                    delay = self._retry_after_seconds(retry_after, attempt)
                    if attempt >= self.max_retries:
                        raise MexcRateLimitError(f"MEXC rate limit at {path}; retries exhausted")
                    await asyncio.sleep(delay + random.uniform(0.0, min(0.25, delay / 4)))
                    continue
                response.raise_for_status()
                date_header = response.headers.get("Date")
                if date_header:
                    try:
                        self._last_server_time = int(parsedate_to_datetime(date_header).timestamp())
                    except (TypeError, ValueError, OverflowError):
                        pass
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise MexcRequestError(f"MEXC returned non-JSON data from {path}") from exc
                if not self._response_ok(payload):
                    code = payload.get("code") if isinstance(payload, dict) else None
                    message = payload.get("message", payload.get("msg", "unknown error")) if isinstance(payload, dict) else "unknown error"
                    raise MexcRequestError(f"MEXC logical error at {path}: code={code}, message={message}")
                return payload
            except MexcRateLimitError:
                raise
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError, MexcRequestError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                # Do not retry client errors except transient 408/425/429/5xx.
                if isinstance(exc, httpx.HTTPStatusError) and status is not None and status < 500 and status not in (408, 425):
                    raise MexcRequestError(f"MEXC HTTP {status} at {path}") from exc
                if isinstance(exc, MexcRequestError) and not isinstance(exc, MexcRateLimitError):
                    raise
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(min(0.35 * (2**attempt), 4.0) + random.uniform(0.0, 0.15))
        raise MexcRequestError(f"MEXC request failed at {path}: {type(last_error).__name__ if last_error else 'unknown'}") from last_error

    async def server_time(self) -> int:
        payload = await self._get("/api/v1/contract/ping", family="server_time")
        # Some Futures responses return {success:true, data:<timestamp>}; the endpoint can
        # also act as a health check. A missing timestamp is unresolved, not fabricated.
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, (int, float, str)):
            stamp = int(float(data))
            self.server_time_source = "payload.data"
            return stamp // 1000 if stamp > 100_000_000_000 else stamp
        if isinstance(payload, dict):
            for key in ("serverTime", "server_time", "time", "timestamp"):
                if key in payload:
                    stamp = int(float(payload[key]))
                    self.server_time_source = f"payload.{key}"
                    return stamp // 1000 if stamp > 100_000_000_000 else stamp
        if self._last_server_time is not None:
            # HTTP Date is a one-second-resolution server/proxy clock, not a substitute for
            # explicit exchange-time fields. Callers should record this lower-confidence source.
            self.server_time_source = "http_date_header"
            return self._last_server_time
        raise MexcRequestError("Server-time endpoint and HTTP Date header did not provide a timestamp")

    async def get_klines(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: int | None = None,
        end: int | None = None,
        now_epoch: int | None = None,
        close_grace_seconds: int = 5,
        include_incomplete: bool = False,
    ) -> list[Candle]:
        symbol = normalize_symbol(symbol)
        tf = timeframe.upper()
        mexc_interval = {
            "1M": "Min1", "5M": "Min5", "15M": "Min15", "30M": "Min30",
            "1H": "Min60", "4H": "Hour4", "8H": "Hour8", "1D": "Day1",
            "1W": "Week1", "1MO": "Month1",
        }.get(tf)
        if mexc_interval is None:
            if tf in {"3M", "12H", "3D"}:
                raise ValueError(f"MEXC has no documented native {tf} interval; derive it from completed source bars")
            raise ValueError(f"Unsupported MEXC interval for {timeframe}")
        params: dict[str, Any] = {"interval": mexc_interval}
        if start is not None:
            params["start"] = int(start)
        if end is not None:
            params["end"] = int(end)
        payload = await self._get(f"/api/v1/contract/kline/{symbol}", params=params, family="kline")
        candles = parse_kline_payload(payload, symbol, tf)
        if start is not None:
            candles = [c for c in candles if c.open_time >= start]
        if end is not None:
            candles = [c for c in candles if c.open_time <= end]
        if now_epoch is None:
            now_epoch = await self.server_time()
        closed_before = now_epoch - close_grace_seconds
        if include_incomplete:
            from dataclasses import replace
            candles = [replace(c, complete=(c.close_time <= closed_before)) for c in candles]
        else:
            candles = [c for c in candles if c.close_time <= closed_before]
        return candles

    async def get_klines_window(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: int,
        end: int,
        now_epoch: int | None = None,
        close_grace_seconds: int = 5,
        max_bars_per_request: int = 1800,
    ) -> list[Candle]:
        """Fetch a history window in bounded segments, deduplicating at boundaries."""
        tf = timeframe.upper()
        if tf in {"3M", "12H", "3D"}:
            if tf == "3M":
                source = await self.get_klines_window(symbol, "1M", start=start, end=end, now_epoch=now_epoch,
                    close_grace_seconds=close_grace_seconds, max_bars_per_request=max_bars_per_request)
                return aggregate_3m_from_1m(source)
            if tf == "12H":
                source = await self.get_klines_window(symbol, "4H", start=start, end=end, now_epoch=now_epoch,
                    close_grace_seconds=close_grace_seconds, max_bars_per_request=max_bars_per_request)
                return aggregate_12h_from_4h(source)
            source = await self.get_klines_window(symbol, "1D", start=start, end=end, now_epoch=now_epoch,
                close_grace_seconds=close_grace_seconds, max_bars_per_request=max_bars_per_request)
            return aggregate_3d_from_1d(source)
        if tf not in {"1M", "5M", "15M", "30M", "1H", "4H", "8H", "1D", "1W", "1MO"}:
            raise ValueError("Unsupported native MEXC interval or derived interval")
        if end < start:
            raise ValueError("end must be >= start")
        if not 1 <= max_bars_per_request <= 2000:
            raise ValueError("max_bars_per_request must be 1..2000")
        step = INTERVAL_SECONDS[tf]
        # Calendar-month bars have unequal durations; one bounded request is used for charts.
        if tf == "1MO":
            return await self.get_klines(symbol, tf, start=start, end=end, now_epoch=now_epoch,
                close_grace_seconds=close_grace_seconds, include_incomplete=False)
        chunk_span = step * (max_bars_per_request - 1)
        cursor = int(start)
        by_time: dict[int, Candle] = {}
        while cursor <= end:
            chunk_end = min(end, cursor + chunk_span)
            chunk = await self.get_klines(
                symbol, tf, start=cursor, end=chunk_end, now_epoch=now_epoch,
                close_grace_seconds=close_grace_seconds, include_incomplete=False,
            )
            for candle in chunk:
                if start <= candle.open_time <= end:
                    prior = by_time.get(candle.open_time)
                    if prior is not None and prior != candle:
                        raise MexcRequestError(f"MEXC returned conflicting duplicate candle for {symbol} at {candle.open_time}")
                    by_time[candle.open_time] = candle
            if chunk_end >= end:
                break
            cursor = chunk_end + step
        return [by_time[t] for t in sorted(by_time)]

    async def contract_details(self) -> list[dict[str, Any]]:
        """Return contract metadata, caching the full-list endpoint briefly.

        Concurrent symbol lookups share one request. Entries expire after a short TTL so
        changing contract availability is not cached indefinitely. Copies prevent callers
        from mutating the shared cached rows.
        """
        async with self._contract_details_cache_lock:
            now = time.monotonic()
            cached = self._contract_details_cache
            if cached is not None and now - cached[0] < self._contract_details_cache_ttl:
                return [dict(row) for row in cached[1]]

            payload = await self._get("/api/v1/contract/detail/country", family="contract")
            data = payload.get("data", []) if isinstance(payload, dict) else []
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                raise MexcRequestError("Unexpected contract detail payload")
            rows = tuple(dict(row) for row in data if isinstance(row, dict))
            self._contract_details_cache = (time.monotonic(), rows)
            return [dict(row) for row in rows]

    async def contract_detail(self, symbol: str) -> dict[str, Any] | None:
        symbol = normalize_symbol(symbol)
        rows = await self.contract_details()
        for row in rows:
            candidate = row.get("symbol") or row.get("contractCode")
            try:
                if candidate and normalize_symbol(str(candidate)) == symbol:
                    return row
            except ValueError:
                continue
        return None

    async def ticker(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        payload = await self._get("/api/v1/contract/ticker", params={"symbol": symbol}, family="ticker")
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(data, list):
            matches = [x for x in data if isinstance(x, dict) and str(x.get("symbol", "")).upper() == symbol]
            if not matches:
                raise MexcRequestError(f"No ticker returned for {symbol}")
            return matches[0]
        if not isinstance(data, dict):
            raise MexcRequestError("Unexpected ticker payload")
        if data.get("symbol") and str(data["symbol"]).upper() != symbol:
            raise MexcRequestError("Ticker symbol mismatch")
        return data

    async def all_tickers(self) -> list[dict[str, Any]]:
        """Fetch the public 24h ticker snapshot for all MEXC Futures contracts."""
        payload = await self._get("/api/v1/contract/ticker", family="ticker")
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise MexcRequestError("Unexpected all-contract ticker payload")
        return [dict(row) for row in data if isinstance(row, dict)]

    async def depth(self, symbol: str, *, limit: int = 20) -> dict[str, Any]:
        """Fetch the current MEXC Futures order book for execution-safety research only."""
        symbol = normalize_symbol(symbol)
        if not 1 <= limit <= 50:
            raise ValueError("depth limit must be 1..50")
        payload = await self._get(
            f"/api/v1/contract/depth/{symbol}", params={"limit": limit}, family="depth"
        )
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            raise MexcRequestError("Unexpected depth payload")
        return data

    async def recent_trades(self, symbol: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent public Futures trades; never used as an extra strategy candle timeframe."""
        symbol = normalize_symbol(symbol)
        if not 1 <= limit <= 1000:
            raise ValueError("trade limit must be 1..1000")
        payload = await self._get(
            f"/api/v1/contract/deals/{symbol}", params={"limit": limit}, family="trades"
        )
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            raise MexcRequestError("Unexpected recent-trades payload")
        return [row for row in data if isinstance(row, dict)]

    async def funding_current(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        payload = await self._get(f"/api/v1/contract/funding_rate/{symbol}", family="funding")
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if isinstance(data, list):
            if not data or not isinstance(data[0], dict):
                raise MexcRequestError("Unexpected funding payload")
            data = data[0]
        if not isinstance(data, dict):
            raise MexcRequestError("Unexpected funding payload")
        return data

    async def funding_history(self, symbol: str, *, page_num: int = 1, page_size: int = 100) -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        if not 1 <= page_size <= 1000 or page_num < 1:
            raise ValueError("Invalid funding history pagination")
        payload = await self._get(
            "/api/v1/contract/funding_rate/history",
            params={"symbol": symbol, "page_num": page_num, "page_size": page_size},
            family="funding",
        )
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if isinstance(data, dict):
            data = data.get("resultList", data.get("list", data.get("records", [])))
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]

    async def funding_history_window(self, symbol: str, *, start: int, end: int, max_pages: int = 30) -> list[dict[str, Any]]:
        """Fetch funding history pages until the requested range is covered or page cap is hit.

        Page ordering and endpoint retention are exchange-controlled; results are filtered by
        settlement time and a warning is required if the oldest retrieved item does not reach start.
        """
        symbol = normalize_symbol(symbol)
        collected: dict[int, dict[str, Any]] = {}
        for page in range(1, max_pages + 1):
            rows = await self.funding_history(symbol, page_num=page, page_size=100)
            if not rows:
                break
            normalized_page_times: list[int] = []
            for row in rows:
                stamp_raw = row.get("settleTime", row.get("settle_time", row.get("fundingTime", 0)))
                try:
                    stamp = int(float(stamp_raw))
                except (TypeError, ValueError, OverflowError):
                    continue
                # MEXC documents settleTime as a Unix-millisecond timestamp.  Convert
                # once and use that same seconds value for collection AND pagination.
                if stamp >= 100_000_000_000:
                    stamp //= 1000
                normalized_page_times.append(stamp)
                row_copy = dict(row)
                row_copy["settleTime"] = stamp
                if start < stamp <= end:
                    collected[stamp] = row_copy
            # MEXC's funding-history endpoint is normally newest-first, but do not
            # assume page row order when deciding that the requested interval is covered.
            # Infer this page's order from its endpoints so ascending responses are also
            # handled correctly. All comparisons are Unix seconds after normalization.
            if len(rows) < 100:
                break
            if len(normalized_page_times) >= 2:
                first_stamp = normalized_page_times[0]
                last_stamp = normalized_page_times[-1]
                if first_stamp > last_stamp and min(normalized_page_times) <= start:
                    break  # Newest-first: this page already reaches before the test start.
                if first_stamp < last_stamp and max(normalized_page_times) >= end:
                    break  # Oldest-first: this page has reached the end of the requested range.
        return [collected[t] for t in sorted(collected)]
