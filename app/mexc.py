from __future__ import annotations

import asyncio
import logging
import math
import random
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.models import Candle, ContractInfo

log = logging.getLogger(__name__)


class MexcAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandleBatch:
    candles: list[Candle]
    requested_start: int
    requested_end: int
    expected_count: int
    missing_timestamps: tuple[int, ...]
    duplicate_timestamps: tuple[int, ...]
    invalid_timestamps: tuple[int, ...]
    out_of_order_timestamps: tuple[int, ...]
    request_errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not (self.missing_timestamps or self.duplicate_timestamps or self.invalid_timestamps
                    or self.out_of_order_timestamps or self.request_errors)


def _unwrap(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise MexcAPIError("Unexpected MEXC response type")
    if payload.get("success") is False:
        raise MexcAPIError(f"MEXC returned code={payload.get('code')}")
    if payload.get("code") not in (None, 0, "0"):
        raise MexcAPIError(f"MEXC returned code={payload.get('code')}")
    return payload.get("data", payload)


def _candles_from_payload(payload: Any) -> list[Candle]:
    data = _unwrap(payload)
    rows: list[Candle] = []
    if isinstance(data, dict) and isinstance(data.get("time"), list):
        keys = ("time", "open", "high", "low", "close")
        if any(not isinstance(data.get(k), list) for k in keys):
            raise MexcAPIError("MEXC kline arrays are malformed")
        n = min(len(data[k]) for k in keys)
        volumes = data.get("vol", [0.0] * n)
        if not isinstance(volumes, list):
            volumes = [0.0] * n
        for i in range(n):
            try:
                rows.append(Candle(
                    time=int(data["time"][i]), open=float(data["open"][i]),
                    high=float(data["high"][i]), low=float(data["low"][i]),
                    close=float(data["close"][i]), volume=float(volumes[i]) if i < len(volumes) else 0.0,
                ))
            except (TypeError, ValueError, OverflowError):
                continue
    elif isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                rows.append(Candle(
                    time=int(row.get("time", row.get("t"))),
                    open=float(row.get("open", row.get("o"))),
                    high=float(row.get("high", row.get("h"))),
                    low=float(row.get("low", row.get("l"))),
                    close=float(row.get("close", row.get("c"))),
                    volume=float(row.get("vol", row.get("volume", 0.0))),
                ))
            except (TypeError, ValueError, OverflowError):
                continue
    else:
        raise MexcAPIError("MEXC candle response does not contain supported data format")
    return rows


class MexcClient:
    MAX_POINTS = 2000
    INTERVAL_SECONDS = 60

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.mexc_timeout_seconds),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"User-Agent": "BTC-MEXC-Signal-Backtest/1.0"},
        )
        self._owns_client = client is None
        self._throttle_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.settings.mexc_base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.settings.mexc_max_retries + 1):
            try:
                async with self._throttle_lock:
                    wait = 0.12 - (asyncio.get_running_loop().time() - self._last_request_at)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last_request_at = asyncio.get_running_loop().time()
                response = await self._client.get(url, params=params)
                transient = response.status_code == 429 or response.status_code >= 500
                if transient:
                    if attempt < self.settings.mexc_max_retries:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = min(8.0, float(retry_after)) if retry_after else min(8.0, 0.5 * (2**attempt))
                        except ValueError:
                            delay = min(8.0, 0.5 * (2**attempt))
                        await asyncio.sleep(delay + random.uniform(0, 0.15))
                        continue
                    raise MexcAPIError(f"MEXC transient HTTP status {response.status_code}")
                if response.status_code >= 400:
                    raise MexcAPIError(f"MEXC rejected request with HTTP {response.status_code}")
                try:
                    return response.json()
                except ValueError as exc:
                    raise MexcAPIError("MEXC returned invalid JSON") from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.settings.mexc_max_retries:
                    break
                await asyncio.sleep(min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.15))
        raise MexcAPIError(f"MEXC request failed after retries: {type(last_error).__name__ if last_error else 'HTTP error'}")

    async def get_contract_info(self) -> ContractInfo:
        payload = await self._get_json("/api/v1/contract/detail/country", {"symbol": self.settings.mexc_symbol})
        data = _unwrap(payload)
        if isinstance(data, list):
            data = next((item for item in data if item.get("symbol") == self.settings.mexc_symbol), None)
        if not isinstance(data, dict) or data.get("symbol") != self.settings.mexc_symbol:
            raise MexcAPIError("BTC_USDT contract metadata missing from MEXC response")
        try:
            info = ContractInfo(
                symbol=str(data["symbol"]), contract_size=float(data["contractSize"]),
                price_tick=float(data["priceUnit"]), volume_step=float(data["volUnit"]),
                min_volume=float(data["minVol"]), max_volume=float(data["maxVol"]),
                max_leverage=float(data["maxLeverage"]),
                country_max_leverage=float(data.get("countryConfigContractMaxLeverage", 0) or 0),
                taker_fee_rate=float(data["takerFeeRate"]),
                maintenance_margin_rate=float(data["maintenanceMarginRate"]),
                liquidation_fee_rate=float(data["liquidationFeeRate"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MexcAPIError("MEXC contract metadata is incomplete") from exc
        positive = (info.contract_size, info.price_tick, info.volume_step, info.min_volume,
                    info.max_volume, info.max_leverage)
        rates = (info.taker_fee_rate, info.maintenance_margin_rate, info.liquidation_fee_rate)
        if any(not math.isfinite(x) or x <= 0 for x in positive):
            raise MexcAPIError("MEXC contract metadata contains invalid sizing/leverage fields")
        if any(not math.isfinite(x) or x < 0 or x >= 1 for x in rates):
            raise MexcAPIError("MEXC contract metadata contains invalid fee/margin rates")
        if info.country_max_leverage < 0 or not math.isfinite(info.country_max_leverage):
            raise MexcAPIError("MEXC country leverage metadata is invalid")
        return info

    async def get_candle_batch(self, start: int, end: int) -> CandleBatch:
        if start > end or start % 60 or end % 60:
            raise ValueError("Candle range must be UTC minute-aligned and start <= end")
        expected_count = ((end - start) // 60) + 1
        all_rows: list[Candle] = []
        request_errors: list[str] = []
        segment_minutes = self.MAX_POINTS - 1
        cursor = start
        while cursor <= end:
            segment_end = min(end, cursor + segment_minutes * 60)
            try:
                payload = await self._get_json(
                    f"/api/v1/contract/kline/{self.settings.mexc_symbol}",
                    {"interval": self.settings.mexc_interval, "start": cursor, "end": segment_end},
                )
                all_rows.extend(_candles_from_payload(payload))
            except (MexcAPIError, ValueError) as exc:
                request_errors.append(f"segment {cursor}-{segment_end}: {type(exc).__name__}")
            cursor = segment_end + 60  # Continue even when a segment is empty or failed.
        # Preserve duplicate evidence before deduplicating the usable series.
        counts: dict[int, int] = {}
        valid_by_time: dict[int, Candle] = {}
        invalid_times: list[int] = []
        out_of_order: list[int] = []
        previous_time: int | None = None
        for candle in all_rows:
            counts[candle.time] = counts.get(candle.time, 0) + 1
            if previous_time is not None and candle.time < previous_time:
                out_of_order.append(candle.time)
            previous_time = candle.time
            if not candle.is_valid() or candle.time % 60 != 0:
                invalid_times.append(candle.time)
            elif start <= candle.time <= end:
                valid_by_time[candle.time] = candle
        duplicates = tuple(sorted(t for t, count in counts.items() if count > 1))
        expected = set(range(start, end + 1, 60))
        missing = tuple(sorted(expected - set(valid_by_time)))
        candles = [valid_by_time[t] for t in sorted(valid_by_time)]
        invalid = tuple(sorted(set(invalid_times)))
        return CandleBatch(candles, start, end, expected_count, missing, duplicates, invalid,
                           tuple(sorted(set(out_of_order))), tuple(request_errors))

    async def get_recent_closed_candles(self, count: int, now: int) -> list[Candle]:
        latest_closed_open = ((now - 1) // 60) * 60 - 60
        start = latest_closed_open - (count - 1) * 60
        batch = await self.get_candle_batch(start, latest_closed_open)
        if not batch.complete:
            raise MexcAPIError(
                f"Recent candle data incomplete: missing={len(batch.missing_timestamps)}, "
                f"invalid={len(batch.invalid_timestamps)}, out_of_order={len(batch.out_of_order_timestamps)}, "
                f"errors={len(batch.request_errors)}"
            )
        return batch.candles

    async def get_ticker(self) -> tuple[float, int]:
        payload = await self._get_json("/api/v1/contract/ticker", {"symbol": self.settings.mexc_symbol})
        data = _unwrap(payload)
        if isinstance(data, list):
            data = next((item for item in data if item.get("symbol") == self.settings.mexc_symbol), None)
        if not isinstance(data, dict) or data.get("symbol") != self.settings.mexc_symbol:
            raise MexcAPIError("BTC_USDT ticker missing")
        try:
            price = float(data["lastPrice"])
            timestamp = int(data["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MexcAPIError("MEXC ticker price or timestamp is missing/invalid") from exc
        if not math.isfinite(price) or price <= 0 or timestamp <= 0:
            raise MexcAPIError("MEXC ticker returned invalid price/timestamp")
        # Futures ticker timestamps are Unix milliseconds in the documented response.
        timestamp_seconds = timestamp // 1000 if timestamp > 10_000_000_000 else timestamp
        return price, timestamp_seconds
