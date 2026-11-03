from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pandas as pd

from app.config import get_settings
from app.market.candles import TIMEFRAME_SECONDS, closed_candles, validate_candles


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
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self._max_retries:
                        retry_after = float(response.headers.get("Retry-After", 0) or 0)
                        time.sleep(min(max(retry_after, 0.25), 8.0) if retry_after else min(0.4 * 2**attempt, 5.0))
                        continue
                response.raise_for_status()
                body = response.json()
                if isinstance(body, dict) and body.get("success") is False:
                    raise MexcAPIError(f"MEXC returned code={body.get('code')}: {body.get('message')}")
                return body.get("data") if isinstance(body, dict) and "data" in body else body
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                time.sleep(min(0.4 * 2**attempt, 5.0))
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
        """Fetch bounded funding history pages and retain events inside [start_ms, end_ms]."""
        first = self.funding_history(symbol, page_num=1, page_size=page_size) or {}
        total_pages = int(first.get("totalPage") or 1)
        pages = min(max(total_pages, 1), max_pages)
        items = list(first.get("resultList") or [])
        for page_num in range(2, pages + 1):
            page = self.funding_history(symbol, page_num=page_num, page_size=page_size) or {}
            items.extend(page.get("resultList") or [])
        events = []
        for item in items:
            try:
                settle_ms = int(item["settleTime"])
            except (KeyError, TypeError, ValueError):
                continue
            if start_ms <= settle_ms <= end_ms:
                events.append(item)
        events.sort(key=lambda x: int(x["settleTime"]))
        if total_pages > max_pages:
            raise MexcAPIError(
                f"Funding history for {symbol} spans {total_pages} pages; configured safety cap is {max_pages}"
            )
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
                arrays = [data.get(k, []) for k in keys]
                for item in zip(*arrays):
                    ts, op, hi, lo, cl, vol, amount = item
                    rows.append({
                        "timestamp": pd.to_datetime(int(ts), unit="s", utc=True),
                        "open": op, "high": hi, "low": lo, "close": cl,
                        "volume": vol, "amount": amount or 0,
                    })
            elif isinstance(data, list):
                # Defensive parser for a future documented object-list response.
                for item in data:
                    rows.append({
                        "timestamp": pd.to_datetime(item["time"], unit="s", utc=True),
                        "open": item["open"], "high": item["high"], "low": item["low"],
                        "close": item["close"], "volume": item.get("vol", 0),
                        "amount": item.get("amount", 0),
                    })
            cursor = chunk_end + 1
        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "amount"])
        frame = validate_candles(pd.DataFrame(rows))
        return closed_candles(frame, seconds)


def normalize_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper().replace("-", "_").replace("/", "_")
    if "_" not in symbol and symbol.endswith("USDT"):
        symbol = symbol[:-4] + "_USDT"
    if not symbol.endswith("_USDT") or not symbol.replace("_", "").isalnum():
        raise ValueError("Symbol must be a MEXC USDT perpetual, e.g. BTC_USDT")
    return symbol
