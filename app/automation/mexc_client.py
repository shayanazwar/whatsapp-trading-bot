from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from ..config import Settings

LOGGER = logging.getLogger(__name__)


class MexcAPIError(RuntimeError):
    def __init__(self, message: str, *, code: Any = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class MexcOrderResponse:
    order_id: str
    raw: dict[str, Any]


def build_query_string(params: Mapping[str, Any] | None) -> str:
    if not params:
        return ""
    clean = [(str(k), v) for k, v in params.items() if v is not None]
    clean.sort(key=lambda item: item[0])
    return urlencode(clean, doseq=True)


def build_signature(access_key: str, secret_key: str, timestamp: str, parameter_string: str) -> str:
    target = f"{access_key}{timestamp}{parameter_string}"
    return hmac.new(
        secret_key.encode("utf-8"),
        target.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _normalize_timestamp_ms(value: int | float) -> int:
    timestamp = int(value)
    return timestamp if timestamp >= 10**12 else timestamp * 1000


class MexcClient:
    """Small async client for the current MEXC Futures REST API."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.mexc_api_base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=20)
        self._server_offset_ms = 0
        self._server_offset_initialized = False

    async def close(self) -> None:
        await self.http.aclose()

    async def _server_time_ms(self) -> int:
        response = await self.http.get(f"{self.base_url}/api/v1/contract/ping")
        response.raise_for_status()
        payload = response.json()
        value = payload.get("data")
        if value is None:
            raise MexcAPIError(f"MEXC server-time response missing data: {payload}")
        return int(value)

    async def sync_time(self) -> int:
        before = int(time.time() * 1000)
        server = await self._server_time_ms()
        after = int(time.time() * 1000)
        midpoint = (before + after) // 2
        self._server_offset_ms = server - midpoint
        self._server_offset_initialized = True
        return server

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        private: bool = False,
    ) -> Any:
        method = method.upper()
        params = dict(params or {})
        headers = {"Language": "English"}

        body_text: str | None = None
        if json_body is not None:
            # The exact JSON string is signed and sent for POST requests.
            body_text = json.dumps(
                dict(json_body),
                separators=(",", ":"),
                ensure_ascii=False,
            )
            headers["Content-Type"] = "application/json"

        if private:
            if not self.settings.mexc_access_key or not self.settings.mexc_secret_key:
                raise MexcAPIError("MEXC private API credentials are not configured")

            if not self._server_offset_initialized:
                try:
                    await self.sync_time()
                except Exception as exc:
                    raise MexcAPIError(f"Could not synchronize MEXC server time: {exc}") from exc

            timestamp = str(int(time.time() * 1000) + self._server_offset_ms)
            parameter_string = body_text if method == "POST" else build_query_string(params)
            headers.update(
                {
                    "ApiKey": self.settings.mexc_access_key,
                    "Request-Time": timestamp,
                    "Signature": build_signature(
                        self.settings.mexc_access_key,
                        self.settings.mexc_secret_key,
                        timestamp,
                        parameter_string,
                    ),
                    "Recv-Window": str(max(1, min(self.settings.mexc_recv_window, 60))),
                }
            )

        request_kwargs: dict[str, Any] = {
            "method": method,
            "url": f"{self.base_url}{path}",
            "headers": headers,
        }
        if params:
            request_kwargs["params"] = params
        if body_text is not None:
            request_kwargs["content"] = body_text

        try:
            response = await self.http.request(**request_kwargs)
        except httpx.HTTPError as exc:
            raise MexcAPIError(f"MEXC HTTP request failed: {exc}") from exc

        if response.is_error:
            raise MexcAPIError(
                f"MEXC HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise MexcAPIError("MEXC returned non-JSON data") from exc

        if isinstance(payload, dict) and payload.get("success") is False:
            raise MexcAPIError(
                str(payload.get("message") or "MEXC request failed"),
                code=payload.get("code"),
            )

        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    async def get_contracts(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/contract/detail/country")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            # Some responses may return one contract when a symbol filter is used.
            if "symbol" in data:
                return [data]
        raise MexcAPIError(f"Unexpected MEXC contract response shape: {type(data).__name__}")

    async def get_tickers(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/contract/ticker")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict) and "symbol" in data:
            return [data]
        raise MexcAPIError(f"Unexpected MEXC ticker response shape: {type(data).__name__}")

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        data = await self._request(
            "GET",
            "/api/v1/contract/ticker",
            params={"symbol": symbol},
        )
        if not isinstance(data, dict):
            raise MexcAPIError(f"Unexpected MEXC ticker response shape: {type(data).__name__}")
        return data

    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list[list[float | int]]:
        interval_seconds = {
            "Min1": 60,
            "Min5": 300,
            "Min15": 900,
            "Min30": 1800,
            "Min60": 3600,
            "Hour4": 14400,
            "Hour8": 28800,
            "Day1": 86400,
            "Week1": 604800,
            "Month1": 2592000,
        }.get(interval)
        if interval_seconds is None:
            raise ValueError(f"Unsupported MEXC interval: {interval}")

        now_sec = int(time.time())
        points = max(10, min(limit, 2000))
        # MEXC's current kline endpoint documents start/end rather than a
        # `limit` request parameter. Fetch a small bounded window instead of
        # downloading the full 2000-point default.
        start_sec = now_sec - (interval_seconds * (points + 3))
        data = await self._request(
            "GET",
            f"/api/v1/contract/kline/{symbol}",
            params={"interval": interval, "start": start_sec, "end": now_sec},
        )
        if not isinstance(data, dict):
            raise MexcAPIError(f"Unexpected MEXC kline response shape: {type(data).__name__}")

        keys = ("time", "open", "high", "low", "close", "vol")
        if not all(key in data for key in keys):
            raise MexcAPIError("MEXC kline response is missing required arrays")

        arrays = [data[key] for key in keys]
        size = min(len(values) for values in arrays)
        if size == 0:
            return []

        rows: list[list[float | int]] = []
        for i in range(size):
            rows.append(
                [
                    _normalize_timestamp_ms(arrays[0][i]),
                    float(arrays[1][i]),
                    float(arrays[2][i]),
                    float(arrays[3][i]),
                    float(arrays[4][i]),
                    float(arrays[5][i]),
                ]
            )

        rows.sort(key=lambda row: int(row[0]))
        return rows[-max(1, min(limit, 2000)) :]

    async def get_account_assets(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "/api/v1/private/account/assets",
            private=True,
        )
        if not isinstance(data, list):
            raise MexcAPIError(f"Unexpected MEXC account asset response: {data!r}")
        return [x for x in data if isinstance(x, dict)]

    async def get_open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        data = await self._request(
            "GET",
            "/api/v1/private/position/open_positions",
            params=params,
            private=True,
        )
        if not isinstance(data, list):
            raise MexcAPIError(f"Unexpected MEXC positions response: {data!r}")
        return [x for x in data if isinstance(x, dict)]

    async def get_position_mode(self) -> Any:
        return await self._request(
            "GET",
            "/api/v1/private/position/position_mode",
            private=True,
        )

    async def change_leverage(self, payload: Mapping[str, Any]) -> dict[str, Any] | Any:
        data = await self._request(
            "POST",
            "/api/v1/private/position/change_leverage",
            json_body=payload,
            private=True,
        )
        return data

    async def place_order(self, payload: Mapping[str, Any]) -> MexcOrderResponse:
        data = await self._request(
            "POST",
            "/api/v1/private/order/create",
            json_body=payload,
            private=True,
        )
        if not isinstance(data, dict) or not data.get("orderId"):
            raise MexcAPIError(f"MEXC order response missing orderId: {data!r}")
        return MexcOrderResponse(order_id=str(data["orderId"]), raw=data)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        data = await self._request(
            "GET",
            f"/api/v1/private/order/get/{order_id}",
            private=True,
        )
        if not isinstance(data, dict):
            raise MexcAPIError(f"Unexpected MEXC order response: {data!r}")
        return data

    async def place_position_tpsl(self, payload: Mapping[str, Any]) -> Any:
        return await self._request(
            "POST",
            "/api/v1/private/stoporder/place",
            json_body=payload,
            private=True,
        )
