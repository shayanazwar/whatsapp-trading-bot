from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pandas as pd

from rslr.config import settings
from rslr.models import Contract

log = logging.getLogger(__name__)


class MEXCError(RuntimeError):
    """Raised when MEXC returns an invalid or unsuccessful response."""


class MEXCRestClient:
    """REST-first public MEXC USDT-M Futures adapter.

    Public market endpoints need no API key. Private trading endpoints are deliberately
    not implemented in this client.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or settings.mexc_base_url).rstrip("/")
        self.timeout = timeout or settings.mexc_timeout_seconds
        self.max_retries = settings.mexc_max_retries if max_retries is None else max_retries
        self._client = client or httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": "RSLR-Trading-Bot/0.1.0", "Accept": "application/json"},
        )
        self._owns_client = client is None
        self._last_request_at = 0.0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MEXCRestClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            # Stay comfortably under the documented per-route public rate limits.
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < 0.12:
                time.sleep(0.12 - elapsed)
            try:
                response = self._client.get(url, params=params)
                self._last_request_at = time.monotonic()
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise MEXCError(f"MEXC returned unsuccessful response: {payload.get('code')} {payload.get('message', '')}")
                if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0"):
                    raise MEXCError(f"MEXC error code {payload.get('code')}: {payload.get('message', '')}")
                return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
            except (httpx.HTTPError, ValueError, MEXCError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = min(0.6 * (2**attempt), 5.0) + random.uniform(0, 0.15)
                log.warning("MEXC request failed (%s), retry %s/%s in %.2fs", exc, attempt + 1, self.max_retries, delay)
                time.sleep(delay)
        raise MEXCError(f"MEXC request failed after retries: {url}: {last_error}") from last_error

    def get_contracts(self) -> list[Contract]:
        data = self._get_json("/api/v1/contract/detail/country")
        if isinstance(data, dict):
            rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            raise MEXCError("Unexpected contract-info response shape")
        return [Contract.from_api(row) for row in rows if isinstance(row, dict) and row.get("symbol")]

    def get_tickers(self) -> list[dict[str, Any]]:
        data = self._get_json("/api/v1/contract/ticker")
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise MEXCError("Unexpected ticker response shape")
        return [x for x in data if isinstance(x, dict)]

    def get_universe(self, limit: int | None = None) -> list[Contract]:
        limit = limit or settings.universe_size
        contracts = {c.symbol: c for c in self.get_contracts()}
        tickers = {str(t.get("symbol")): t for t in self.get_tickers()}
        eligible: list[Contract] = []
        for symbol, contract in contracts.items():
            ticker = tickers.get(symbol, {})
            quote_turnover = float(ticker.get("amount24") or 0)
            if (
                symbol.endswith("_USDT")
                and contract.quote_coin == "USDT"
                and contract.state == 0
                and not contract.is_new
                and contract.contract_size > 0
                and quote_turnover >= settings.min_24h_quote_volume
            ):
                eligible.append(
                    Contract(
                        **{**contract.__dict__, "quote_volume_24h": quote_turnover}
                    )
                )
        eligible.sort(key=lambda c: c.quote_volume_24h, reverse=True)
        return eligible[:limit]

    def get_klines(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "Min60",
    ) -> pd.DataFrame:
        if interval not in {"Min60", "Hour4", "Day1"}:
            raise ValueError("Only 1H, 4H and 1D exchange intervals are allowed")
        params = {
            "interval": interval,
            "start": int(start.astimezone(timezone.utc).timestamp()),
            "end": int(end.astimezone(timezone.utc).timestamp()),
        }
        data = self._get_json(f"/api/v1/contract/kline/{symbol}", params=params)
        if not isinstance(data, dict):
            raise MEXCError(f"Unexpected kline response for {symbol}")
        required = ("time", "open", "high", "low", "close", "vol")
        if not all(key in data for key in required):
            raise MEXCError(f"Kline response missing fields for {symbol}")
        lengths = {len(data[k]) for k in required}
        if len(lengths) != 1:
            raise MEXCError(f"Kline arrays have inconsistent lengths for {symbol}")
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(data["time"], unit="s", utc=True),
                "open": pd.to_numeric(data["open"], errors="coerce"),
                "high": pd.to_numeric(data["high"], errors="coerce"),
                "low": pd.to_numeric(data["low"], errors="coerce"),
                "close": pd.to_numeric(data["close"], errors="coerce"),
                "volume": pd.to_numeric(data["vol"], errors="coerce"),
            }
        )
        frame = frame.dropna().sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        now = pd.Timestamp.now(tz="UTC")
        seconds = {"Min60": 3600, "Hour4": 14400, "Day1": 86400}[interval]
        # A candle is complete only after its full interval has elapsed.
        frame = frame[frame["timestamp"] + pd.to_timedelta(seconds, unit="s") <= now].reset_index(drop=True)
        return frame

    def get_funding_history(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        records: list[dict[str, Any]] = []
        page = 1
        while page <= 20:
            data = self._get_json(
                "/api/v1/contract/funding_rate/history",
                params={"symbol": symbol, "page_num": page, "page_size": 1000},
            )
            if not isinstance(data, dict):
                raise MEXCError(f"Unexpected funding response for {symbol}")
            batch = data.get("resultList") or []
            records.extend(x for x in batch if isinstance(x, dict))
            total_pages = int(data.get("totalPage") or page)
            if page >= total_pages or not batch:
                break
            # Pages are newest first; stop once we have crossed the requested start.
            oldest = min((int(x.get("settleTime") or 0) for x in batch), default=0)
            if oldest and oldest < int(start.timestamp() * 1000):
                break
            page += 1
        if not records:
            return pd.DataFrame(columns=["timestamp", "funding_rate"])
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime([int(x["settleTime"]) for x in records], unit="ms", utc=True),
                "funding_rate": [float(x["fundingRate"]) for x in records],
            }
        )
        frame = frame.drop_duplicates("timestamp").sort_values("timestamp")
        return frame[(frame.timestamp >= pd.Timestamp(start)) & (frame.timestamp <= pd.Timestamp(end))].reset_index(drop=True)
