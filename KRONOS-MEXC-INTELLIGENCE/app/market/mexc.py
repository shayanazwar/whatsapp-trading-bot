from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pandas as pd

from app.config import get_settings
from app.market.candles import TIMEFRAME_SECONDS, closed_candles, validate_candles, validate_candle_continuity


class MexcAPIError(RuntimeError):
    pass


class MexcPublicClient:
    """MEXC USDT-M Futures public REST adapter; no private trading credentials."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None, client=None):
        cfg = get_settings()
        self.base_url = (base_url or cfg.mexc_api_base_url).rstrip("/")
        self.timeout = timeout or cfg.mexc_http_timeout
        self._client = client or httpx.Client(timeout=self.timeout)
        self._owns_client = client is None
        self._last_request = 0.0
        self._rate_lock = threading.Lock()
        self._min_interval = cfg.mexc_request_min_interval_seconds
        self._max_retries = cfg.mexc_max_retries

    def close(self):
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _pace(self):
        with self._rate_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None):
        last_error = None
        for attempt in range(self._max_retries + 1):
            self._pace()
            try:
                response = self._client.get(f"{self.base_url}{path}", params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                time.sleep(min(0.4 * 2**attempt, 5.0))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    f"Retryable MEXC HTTP {response.status_code}", request=response.request,
                    response=response,
                )
                if attempt >= self._max_retries:
                    break
                retry_header = response.headers.get("Retry-After", "")
                try:
                    retry_after = float(retry_header) if retry_header else 0.0
                except ValueError:
                    retry_after = 0.0
                time.sleep(min(max(retry_after, 0.25), 8.0) if retry_after else min(0.4 * 2**attempt, 5.0))
                continue

            # Authentication, malformed requests and missing symbols will not
            # become valid by retrying; fail immediately and preserve the status.
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise MexcAPIError(f"Non-retryable MEXC HTTP {response.status_code} for {path}") from exc
            try:
                body = response.json()
            except ValueError as exc:
                raise MexcAPIError(f"MEXC returned invalid JSON for {path}") from exc
            if isinstance(body, dict) and body.get("success") is False:
                raise MexcAPIError(f"MEXC returned code={body.get('code')}: {body.get('message')}")
            return body.get("data") if isinstance(body, dict) and "data" in body else body
        raise MexcAPIError(f"MEXC request failed for {path}: {last_error}") from last_error

    def server_time(self) -> int:
        return int(self._get("/api/v1/contract/ping"))

    def contracts(self, symbol: str | None = None) -> list[dict]:
        data = self._get("/api/v1/contract/detail/country", {"symbol": symbol} if symbol else None)
        if isinstance(data, dict):
            return [data]
        return list(data or [])

    def tickers(self, symbol: str | None = None) -> list[dict]:
        data = self._get("/api/v1/contract/ticker", {"symbol": symbol} if symbol else None)
        if isinstance(data, dict):
            return [data]
        return list(data or [])

    def depth(self, symbol: str, limit: int = 20) -> dict:
        return self._get(f"/api/v1/contract/depth/{symbol}", {"limit": limit})

    def fair_price(self, symbol: str) -> dict:
        return self._get(f"/api/v1/contract/fair_price/{symbol}")

    def funding_rate(self, symbol: str) -> dict:
        return self._get(f"/api/v1/contract/funding_rate/{symbol}")

    def funding_history(self, symbol: str, page_num: int = 1, page_size: int = 100) -> dict:
        return self._get("/api/v1/contract/funding_rate/history", {
            "symbol": symbol, "page_num": page_num, "page_size": min(page_size, 1000),
        })

    def funding_history_range(self, symbol: str, start_ms: int, end_ms: int,
                              *, page_size: int = 1000, max_pages: int = 10) -> list[dict]:
        """Fetch only recent pages needed for [start_ms, end_ms].

        MEXC returns funding history in newest-first order. We stop once a page
        contains a settlement older than the requested start, rather than
        failing merely because the exchange's lifetime history has many pages.
        If the safety page cap is reached before the requested window is covered,
        fail closed so the backtester cannot mistake partial history for complete.
        """
        if start_ms >= end_ms or page_size < 1 or max_pages < 1:
            raise ValueError("Invalid funding-history range or pagination limits")
        first = self.funding_history(symbol, page_num=1, page_size=page_size) or {}
        total_pages = max(1, int(first.get("totalPage") or 1))
        items: list[dict] = []
        page = first
        for page_num in range(1, min(total_pages, max_pages) + 1):
            if page_num > 1:
                page = self.funding_history(symbol, page_num=page_num, page_size=page_size) or {}
            page_items = list(page.get("resultList") or [])
            items.extend(page_items)
            valid_times = []
            for item in page_items:
                try:
                    valid_times.append(int(item["settleTime"]))
                except (KeyError, TypeError, ValueError):
                    continue
            if valid_times and min(valid_times) < start_ms:
                break
            if not page_items:
                if page_num < total_pages:
                    raise MexcAPIError(
                        f"Unexpected empty funding-history page {page_num} of {total_pages} for {symbol}"
                    )
                break
            if page_num == total_pages:
                break
        else:
            page_num = min(total_pages, max_pages)

        # If we stopped at the configured cap while all observations remained
        # newer than the requested start, older pages may contain needed events.
        if page_num >= max_pages and total_pages > max_pages:
            oldest = []
            for item in items:
                try:
                    oldest.append(int(item["settleTime"]))
                except (KeyError, TypeError, ValueError):
                    continue
            if not oldest or min(oldest) >= start_ms:
                raise MexcAPIError(
                    f"Funding history for {symbol} was not fully covered within {max_pages} pages"
                )

        events = []
        for item in items:
            try:
                settle_ms = int(item["settleTime"])
                rate = float(item["fundingRate"])
            except (KeyError, TypeError, ValueError, OverflowError):
                # An unparseable row inside the selected page may be in-range;
                # do not silently omit it and claim complete funding coverage.
                raise MexcAPIError(f"Malformed funding-history row for {symbol}: {item!r}")
            if not (-1 < rate < 1):
                raise MexcAPIError(f"Invalid funding rate for {symbol}: {rate}")
            if start_ms <= settle_ms <= end_ms:
                events.append(item)
        events.sort(key=lambda x: int(x["settleTime"]))
        return events

    def klines(self, symbol: str, interval: str = "Min60", start: int | None = None,
               end: int | None = None) -> pd.DataFrame:
        if interval not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unsupported documented MEXC interval: {interval}")
        seconds = TIMEFRAME_SECONDS[interval]
        now = int(datetime.now(timezone.utc).timestamp())
        start = int(start if start is not None else now - 2000 * seconds)
        end = int(end if end is not None else now)
        if start >= end:
            raise ValueError("start must be before end")
        rows = []
        # MEXC documents a 2,000-point maximum. Use <2,000 point windows to avoid truncation.
        chunk_seconds = 1900 * seconds
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + chunk_seconds - 1)
            data = self._get(f"/api/v1/contract/kline/{symbol}", {
                "interval": interval, "start": cursor, "end": chunk_end,
            })
            if isinstance(data, dict):
                keys = ["time", "open", "high", "low", "close", "vol", "amount"]
                missing = [key for key in keys if key not in data or not isinstance(data[key], list)]
                if missing:
                    raise MexcAPIError(f"Malformed MEXC Kline payload for {symbol}: missing arrays {missing}")
                arrays = [data[key] for key in keys]
                lengths = {key: len(values) for key, values in zip(keys, arrays)}
                if len(set(lengths.values())) != 1:
                    raise MexcAPIError(f"Malformed MEXC Kline payload for {symbol}: unequal array lengths {lengths}")
                # Empty pages are valid at the end of the requested range. Do not
                # `continue` before advancing the cursor or this loop can hang forever.
                for ts, op, hi, lo, cl, vol, amount in zip(*arrays, strict=True):
                    if any(value is None for value in (ts, op, hi, lo, cl, vol, amount)):
                        raise MexcAPIError(f"Malformed MEXC Kline payload for {symbol}: null candle field")
                    rows.append({
                        "timestamp": pd.to_datetime(int(ts), unit="s", utc=True),
                        "open": op, "high": hi, "low": lo, "close": cl,
                        "volume": vol, "amount": amount,
                    })
            elif isinstance(data, list):
                # Strict parser: silently substituting zero volume/amount corrupts
                # technical indicators and Kronos inputs.
                required = {"time", "open", "high", "low", "close", "vol", "amount"}
                for item in data:
                    missing = required - set(item)
                    if missing or any(item.get(key) is None for key in required):
                        raise MexcAPIError(
                            f"Malformed MEXC Kline object for {symbol}: missing/null {sorted(missing)}"
                        )
                    rows.append({
                        "timestamp": pd.to_datetime(int(item["time"]), unit="s", utc=True),
                        "open": item["open"], "high": item["high"], "low": item["low"],
                        "close": item["close"], "volume": item["vol"],
                        "amount": item["amount"],
                    })
            cursor = chunk_end + 1
        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "amount"])
        frame = validate_candles(pd.DataFrame(rows), deduplicate=False, reject_duplicates=True)
        frame = closed_candles(frame, seconds)
        if not frame.empty:
            frame = validate_candle_continuity(frame, seconds)
        return frame


def normalize_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper().replace("-", "_").replace("/", "_")
    if "_" not in symbol and symbol.endswith("USDT"):
        symbol = symbol[:-4] + "_USDT"
    if not symbol.endswith("_USDT") or not symbol.replace("_", "").isalnum():
        raise ValueError("Symbol must be a MEXC USDT perpetual, e.g. BTC_USDT")
    return symbol
