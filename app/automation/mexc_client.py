from __future__ import annotations

import asyncio
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
    def __init__(
        self,
        message: str,
        *,
        code: Any = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class MexcOrderResponse:
    order_id: str
    raw: dict[str, Any]


def build_query_string(
    params: Mapping[str, Any] | None,
) -> str:
    if not params:
        return ""

    clean = [
        (str(k), v)
        for k, v in params.items()
        if v is not None
    ]

    clean.sort(key=lambda item: item[0])

    return urlencode(clean, doseq=True)


def build_signature(
    access_key: str,
    secret_key: str,
    timestamp: str,
    parameter_string: str,
) -> str:
    target = (
        f"{access_key}"
        f"{timestamp}"
        f"{parameter_string}"
    )

    return hmac.new(
        secret_key.encode("utf-8"),
        target.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _normalize_timestamp_ms(
    value: int | float,
) -> int:
    timestamp = int(value)

    return (
        timestamp
        if timestamp >= 10**12
        else timestamp * 1000
    )


class MexcClient:
    """
    Async client for the MEXC Futures REST API.

    Live trading remains controlled by the higher-level execution
    safety flags. This client only provides API communication.
    """

    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings

        self.base_url = (
            settings.mexc_api_base_url.rstrip("/")
        )

        self.http = httpx.AsyncClient(
            timeout=20
        )

        self._server_offset_ms = 0
        self._server_offset_initialized = False

        # --------------------------------------------------------------
        # PUBLIC API RATE LIMITER
        # --------------------------------------------------------------

        self._public_request_lock = (
            asyncio.Lock()
        )

        self._last_public_request = 0.0

        # Minimum spacing between public requests.
        self._public_request_gap = 0.15

    # ==================================================================
    # CONNECTION
    # ==================================================================

    async def close(self) -> None:
        await self.http.aclose()

    # ==================================================================
    # SERVER TIME
    # ==================================================================

    async def _server_time_ms(self) -> int:
        response = await self.http.get(
            f"{self.base_url}/api/v1/contract/ping"
        )

        response.raise_for_status()

        payload = response.json()

        value = payload.get("data")

        if value is None:
            raise MexcAPIError(
                "MEXC server-time response missing data: "
                f"{payload}"
            )

        return int(value)

    async def sync_time(self) -> int:
        before = int(
            time.time() * 1000
        )

        server = await self._server_time_ms()

        after = int(
            time.time() * 1000
        )

        midpoint = (
            before + after
        ) // 2

        self._server_offset_ms = (
            server - midpoint
        )

        self._server_offset_initialized = True

        return server

    # ==================================================================
    # PUBLIC REQUEST HANDLER
    # ==================================================================

    async def _public_request(
        self,
        request_kwargs: dict[str, Any],
    ) -> httpx.Response:
        """
        Shared public-request queue.

        Handles:
        - HTTP 429
        - MEXC HTTP 200 rate-limit responses
        - temporary request failures
        """

        max_attempts = 4

        for attempt in range(max_attempts):

            async with self._public_request_lock:

                now = time.monotonic()

                wait = (
                    self._public_request_gap
                    - (
                        now
                        - self._last_public_request
                    )
                )

                if wait > 0:
                    await asyncio.sleep(wait)

                try:
                    response = await self.http.request(
                        **request_kwargs
                    )

                except httpx.HTTPError as exc:
                    raise MexcAPIError(
                        f"MEXC HTTP request failed: {exc}"
                    ) from exc

                self._last_public_request = (
                    time.monotonic()
                )

            # ----------------------------------------------------------
            # HTTP 429
            # ----------------------------------------------------------

            if response.status_code == 429:

                if attempt >= max_attempts - 1:
                    raise MexcAPIError(
                        f"MEXC HTTP 429: "
                        f"{response.text}",
                        status_code=429,
                    )

                retry_delay = (
                    1.0 * (attempt + 1)
                )

                LOGGER.warning(
                    "MEXC rate limit HTTP 429. "
                    "Retrying in %.1fs "
                    "(attempt %d/%d)",
                    retry_delay,
                    attempt + 1,
                    max_attempts,
                )

                await asyncio.sleep(
                    retry_delay
                )

                continue

            # ----------------------------------------------------------
            # HTTP 200 + success=false + too frequent
            # ----------------------------------------------------------

            try:
                payload = response.json()

            except ValueError:
                payload = None

            if isinstance(payload, dict):

                message = str(
                    payload.get("message")
                    or ""
                ).lower()

                if (
                    payload.get("success") is False
                    and "too frequent" in message
                ):

                    if attempt >= max_attempts - 1:
                        raise MexcAPIError(
                            str(
                                payload.get("message")
                                or "MEXC request rate limited"
                            ),
                            code=payload.get("code"),
                        )

                    retry_delay = (
                        1.0 * (attempt + 1)
                    )

                    LOGGER.warning(
                        "MEXC public API rate limited. "
                        "Retrying in %.1fs "
                        "(attempt %d/%d)",
                        retry_delay,
                        attempt + 1,
                        max_attempts,
                    )

                    await asyncio.sleep(
                        retry_delay
                    )

                    continue

            return response

        raise MexcAPIError(
            "MEXC public request failed "
            "after retries"
        )

    # ==================================================================
    # GENERIC REQUEST
    # ==================================================================

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

        params = dict(
            params or {}
        )

        headers = {
            "Language": "English",
        }

        body_text: str | None = None

        # --------------------------------------------------------------
        # JSON BODY
        # --------------------------------------------------------------

        if json_body is not None:

            body_text = json.dumps(
                dict(json_body),
                separators=(",", ":"),
                ensure_ascii=False,
            )

            headers[
                "Content-Type"
            ] = "application/json"

        # --------------------------------------------------------------
        # PRIVATE AUTHENTICATION
        # --------------------------------------------------------------

        if private:

            if (
                not self.settings.mexc_access_key
                or not self.settings.mexc_secret_key
            ):
                raise MexcAPIError(
                    "MEXC private API credentials "
                    "are not configured"
                )

            if not self._server_offset_initialized:

                try:
                    await self.sync_time()

                except Exception as exc:
                    raise MexcAPIError(
                        "Could not synchronize "
                        f"MEXC server time: {exc}"
                    ) from exc

            timestamp = str(
                int(time.time() * 1000)
                + self._server_offset_ms
            )

            parameter_string = (
                body_text
                if method == "POST"
                else build_query_string(params)
            )

            headers.update(
                {
                    "ApiKey": (
                        self.settings.mexc_access_key
                    ),
                    "Request-Time": timestamp,
                    "Signature": build_signature(
                        self.settings.mexc_access_key,
                        self.settings.mexc_secret_key,
                        timestamp,
                        parameter_string,
                    ),
                    "Recv-Window": str(
                        max(
                            1,
                            min(
                                self.settings.mexc_recv_window,
                                60,
                            ),
                        )
                    ),
                }
            )

        request_kwargs: dict[str, Any] = {
            "method": method,
            "url": (
                f"{self.base_url}{path}"
            ),
            "headers": headers,
        }

        if params:
            request_kwargs[
                "params"
            ] = params

        if body_text is not None:
            request_kwargs[
                "content"
            ] = body_text

        # --------------------------------------------------------------
        # SEND REQUEST
        # --------------------------------------------------------------

        if private:

            try:
                response = await self.http.request(
                    **request_kwargs
                )

            except httpx.HTTPError as exc:
                raise MexcAPIError(
                    f"MEXC HTTP request failed: {exc}"
                ) from exc

        else:
            response = await self._public_request(
                request_kwargs
            )

        # --------------------------------------------------------------
        # HTTP ERROR
        # --------------------------------------------------------------

        if response.is_error:
            raise MexcAPIError(
                f"MEXC HTTP "
                f"{response.status_code}: "
                f"{response.text}",
                status_code=response.status_code,
            )

        # --------------------------------------------------------------
        # JSON
        # --------------------------------------------------------------

        try:
            payload = response.json()

        except ValueError as exc:
            raise MexcAPIError(
                "MEXC returned non-JSON data"
            ) from exc

        # --------------------------------------------------------------
        # MEXC API ERROR
        # --------------------------------------------------------------

        if (
            isinstance(payload, dict)
            and payload.get("success") is False
        ):
            raise MexcAPIError(
                str(
                    payload.get("message")
                    or "MEXC request failed"
                ),
                code=payload.get("code"),
            )

        # --------------------------------------------------------------
        # UNWRAP DATA
        # --------------------------------------------------------------

        if (
            isinstance(payload, dict)
            and "data" in payload
        ):
            return payload["data"]

        return payload

    # ==================================================================
    # CONTRACTS
    # ==================================================================

    async def get_contracts(
        self,
    ) -> list[dict[str, Any]]:

        data = await self._request(
            "GET",
            "/api/v1/contract/detail/country",
        )

        if isinstance(data, list):
            return [
                x
                for x in data
                if isinstance(x, dict)
            ]

        if (
            isinstance(data, dict)
            and "symbol" in data
        ):
            return [data]

        raise MexcAPIError(
            "Unexpected MEXC contract response "
            f"shape: {type(data).__name__}"
        )

    # ==================================================================
    # TICKERS
    # ==================================================================

    async def get_tickers(
        self,
    ) -> list[dict[str, Any]]:

        data = await self._request(
            "GET",
            "/api/v1/contract/ticker",
        )

        if isinstance(data, list):
            return [
                x
                for x in data
                if isinstance(x, dict)
            ]

        if (
            isinstance(data, dict)
            and "symbol" in data
        ):
            return [data]

        raise MexcAPIError(
            "Unexpected MEXC ticker response "
            f"shape: {type(data).__name__}"
        )

    async def get_ticker(
        self,
        symbol: str,
    ) -> dict[str, Any]:

        data = await self._request(
            "GET",
            "/api/v1/contract/ticker",
            params={
                "symbol": symbol,
            },
        )

        if not isinstance(data, dict):
            raise MexcAPIError(
                "Unexpected MEXC ticker response "
                f"shape: {type(data).__name__}"
            )

        return data

    # ==================================================================
    # ORDER BOOK
    # ==================================================================

    async def get_depth(
        self,
        symbol: str,
        limit: int = 20,
    ) -> dict[str, Any]:

        data = await self._request(
            "GET",
            f"/api/v1/contract/depth/{symbol}",
            params={
                "limit": max(
                    1,
                    min(int(limit), 50),
                )
            },
        )

        if not isinstance(data, dict):
            raise MexcAPIError(
                "Unexpected MEXC depth response "
                f"shape: {type(data).__name__}"
            )

        return data

    # ==================================================================
    # INDEX / FAIR PRICE
    # ==================================================================

    async def get_index_price(
        self,
        symbol: str,
    ) -> dict[str, Any]:

        data = await self._request(
            "GET",
            f"/api/v1/contract/index_price/{symbol}",
        )

        if not isinstance(data, dict):
            raise MexcAPIError(
                "Unexpected MEXC index-price response "
                f"shape: {type(data).__name__}"
            )

        return data

    async def get_fair_price(
        self,
        symbol: str,
    ) -> dict[str, Any]:

        data = await self._request(
            "GET",
            f"/api/v1/contract/fair_price/{symbol}",
        )

        if not isinstance(data, dict):
            raise MexcAPIError(
                "Unexpected MEXC fair-price response "
                f"shape: {type(data).__name__}"
            )

        return data

    # ==================================================================
    # FUNDING
    # ==================================================================

    async def get_funding_rate(
        self,
        symbol: str,
    ) -> dict[str, Any]:

        data = await self._request(
            "GET",
            f"/api/v1/contract/funding_rate/{symbol}",
        )

        if not isinstance(data, dict):
            raise MexcAPIError(
                "Unexpected MEXC funding response "
                f"shape: {type(data).__name__}"
            )

        return data

    # ==================================================================
    # RECENT TRADES
    # ==================================================================

    async def get_deals(
        self,
        symbol: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:

        data = await self._request(
            "GET",
            f"/api/v1/contract/deals/{symbol}",
            params={
                "limit": max(
                    1,
                    min(int(limit), 100),
                )
            },
        )

        if not isinstance(data, list):
            raise MexcAPIError(
                "Unexpected MEXC deals response "
                f"shape: {type(data).__name__}"
            )

        return [
            item
            for item in data
            if isinstance(item, dict)
        ]

    # ==================================================================
    # TRADING FEES
    # ==================================================================

    async def get_trading_fee_rate(
        self,
        symbol: str,
    ) -> dict[str, Any]:

        data = await self._request(
            "GET",
            "/api/v1/private/account/tiered_fee_rate",
            params={
                "symbol": symbol,
            },
            private=True,
        )

        if not isinstance(data, dict):
            raise MexcAPIError(
                "Unexpected MEXC fee-rate response "
                f"shape: {type(data).__name__}"
            )

        return data

    # ==================================================================
    # KLINES
    # ==================================================================

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
    ) -> list[list[float | int]]:

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
            raise ValueError(
                f"Unsupported MEXC interval: "
                f"{interval}"
            )

        now_sec = int(
            time.time()
        )

        points = max(
            10,
            min(int(limit), 2000),
        )

        start_sec = (
            now_sec
            - (
                interval_seconds
                * (points + 3)
            )
        )

        data = await self._request(
            "GET",
            f"/api/v1/contract/kline/{symbol}",
            params={
                "interval": interval,
                "start": start_sec,
                "end": now_sec,
            },
        )

        if not isinstance(data, dict):
            raise MexcAPIError(
                "Unexpected MEXC kline response "
                f"shape: {type(data).__name__}"
            )

        keys = (
            "time",
            "open",
            "high",
            "low",
            "close",
            "vol",
        )

        if not all(
            key in data
            for key in keys
        ):
            raise MexcAPIError(
                "MEXC kline response is missing "
                "required arrays"
            )

        arrays = [
            data[key]
            for key in keys
        ]

        size = min(
            len(values)
            for values in arrays
        )

        if size == 0:
            return []

        rows: list[
            list[float | int]
        ] = []

        for i in range(size):

            rows.append(
                [
                    _normalize_timestamp_ms(
                        arrays[0][i]
                    ),
                    float(arrays[1][i]),
                    float(arrays[2][i]),
                    float(arrays[3][i]),
                    float(arrays[4][i]),
                    float(arrays[5][i]),
                ]
            )

        rows.sort(
            key=lambda row: int(row[0])
        )

        return rows[
            -max(
                1,
                min(
                    int(limit),
                    2000,
                ),
            ):
        ]

    # ==================================================================
    # ACCOUNT ASSETS
    # ==================================================================

    async def get_account_assets(
        self,
    ) -> list[dict[str, Any]]:

        data = await self._request(
            "GET",
            "/api/v1/private/account/assets",
            private=True,
        )

        if not isinstance(data, list):
            raise MexcAPIError(
                "Unexpected MEXC account asset "
                f"response: {data!r}"
            )

        return [
            x
            for x in data
            if isinstance(x, dict)
        ]

    async def get_futures_equity(
        self,
    ) -> float:
        """
        Return the USDT futures equity used by
        the future 1% risk-per-trade system.

        IMPORTANT:
        This method intentionally checks the actual
        returned USDT asset fields rather than blindly
        assuming one field name.
        """

        assets = await self.get_account_assets()

        if not assets:
            raise MexcAPIError(
                "MEXC returned no futures account assets"
            )

        usdt_assets = [
            asset
            for asset in assets
            if str(
                asset.get("currency")
                or asset.get("asset")
                or asset.get("coin")
                or ""
            ).upper() == "USDT"
        ]

        if not usdt_assets:
            raise MexcAPIError(
                "USDT futures asset was not found"
            )

        asset = usdt_assets[0]

        candidates = (
            "equity",
            "totalEquity",
            "availableBalance",
            "available",
            "balance",
        )

        for field in candidates:

            value = asset.get(field)

            if value is None:
                continue

            try:
                equity = float(value)

            except (
                TypeError,
                ValueError,
            ):
                continue

            if equity > 0:
                return equity

        raise MexcAPIError(
            "Could not determine USDT futures "
            "equity from MEXC asset response: "
            f"{asset!r}"
        )

    async def get_max_risk_amount(
        self,
        risk_percent: float = 1.0,
    ) -> float:
        """
        Calculate maximum planned risk for one
        future auto-trade.

        Default:
            1% of total futures equity.

        The method refuses values above 1%.
        """

        if risk_percent <= 0:
            raise ValueError(
                "Risk percentage must be positive"
            )

        if risk_percent > 1.0:
            raise ValueError(
                "Auto-trade risk cannot exceed 1% "
                "of total futures equity"
            )

        equity = await self.get_futures_equity()

        risk_amount = (
            equity
            * risk_percent
            / 100.0
        )

        if risk_amount <= 0:
            raise MexcAPIError(
                "Calculated maximum risk is zero"
            )

        return risk_amount

    # ==================================================================
    # OPEN POSITIONS
    # ==================================================================

    async def get_open_positions(
        self,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:

        params = (
            {"symbol": symbol}
            if symbol
            else None
        )

        data = await self._request(
            "GET",
            "/api/v1/private/position/open_positions",
            params=params,
            private=True,
        )

        if not isinstance(data, list):
            raise MexcAPIError(
                "Unexpected MEXC positions response: "
                f"{data!r}"
            )

        return [
            x
            for x in data
            if isinstance(x, dict)
        ]

    # ==================================================================
    # POSITION MODE
    # ==================================================================

    async def get_position_mode(
        self,
    ) -> Any:

        return await self._request(
            "GET",
            "/api/v1/private/position/position_mode",
            private=True,
        )

    # ==================================================================
    # LEVERAGE
    # ==================================================================

    async def change_leverage(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | Any:

        return await self._request(
            "POST",
            "/api/v1/private/position/change_leverage",
            json_body=payload,
            private=True,
        )

    # ==================================================================
    # ORDER CREATION
    # ==================================================================

    async def place_order(
        self,
        payload: Mapping[str, Any],
    ) -> MexcOrderResponse:

        data = await self._request(
            "POST",
            "/api/v1/private/order/create",
            json_body=payload,
            private=True,
        )

        if (
            not isinstance(data, dict)
            or not data.get("orderId")
        ):
            raise MexcAPIError(
                "MEXC order response missing "
                f"orderId: {data!r}"
            )

        return MexcOrderResponse(
            order_id=str(
                data["orderId"]
            ),
            raw=data,
        )

    # ==================================================================
    # ORDER STATUS
    # ==================================================================

    async def get_order(
        self,
        order_id: str,
    ) -> dict[str, Any]:

        data = await self._request(
            "GET",
            f"/api/v1/private/order/get/{order_id}",
            private=True,
        )

        if not isinstance(data, dict):
            raise MexcAPIError(
                "Unexpected MEXC order response: "
                f"{data!r}"
            )

        return data

    # ==================================================================
    # POSITION TP/SL
    # ==================================================================

    async def place_position_tpsl(
        self,
        payload: Mapping[str, Any],
    ) -> Any:

        return await self._request(
            "POST",
            "/api/v1/private/stoporder/place",
            json_body=payload,
            private=True,
        )
