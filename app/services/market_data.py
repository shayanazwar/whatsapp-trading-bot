"""MEXC-only data-loading orchestration with historical warm-up windows."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from app.market.candles import Candle, aggregate_12h_from_4h
from app.mexc.client import MexcClient, MexcError
from app.symbols import normalize_symbol


@dataclass(frozen=True, slots=True)
class SymbolData:
    symbol: str
    timeframes: dict[str, list[Candle]]
    contract: dict[str, Any] | None
    warnings: tuple[str, ...]


class MarketDataService:
    def __init__(self, client: MexcClient, *, close_grace_seconds: int = 5, price_series: str = "real") -> None:
        if price_series not in {"real", "standard"}:
            raise ValueError("price_series must be real or standard")
        self.client = client
        self.close_grace_seconds = close_grace_seconds
        self.price_series = price_series

    async def fetch_symbol(
        self,
        symbol: str,
        *,
        window_days: int = 90,
        daily_warmup_days: int = 400,
        extra_intraday_warmup_days: int = 45,
        now_epoch: int | None = None,
    ) -> SymbolData:
        symbol = normalize_symbol(symbol)
        warnings: list[str] = []
        if window_days < 1 or daily_warmup_days < 200 or extra_intraday_warmup_days < 1:
            raise ValueError("Invalid history window or warm-up")
        if now_epoch is None:
            try:
                now_epoch = await self.client.server_time()
            except MexcError:
                now_epoch = int(time.time())
                warnings.append("Exchange time was not verified; local UTC clock used only to select likely closed bars.")
        now_epoch = int(now_epoch)
        d_start = now_epoch - (window_days + daily_warmup_days) * 86400
        i_start = now_epoch - (window_days + extra_intraday_warmup_days) * 86400
        # Align query starts down to UTC interval boundaries; empirical MEXC timestamp alignment
        # still must be confirmed by the separately provided public-data probe.
        d_start -= d_start % 86400
        h4_start = i_start - (i_start % (4 * 3600))
        h1_start = i_start - (i_start % 3600)
        daily, bars4, bars1 = await self._fetch_parallel(symbol, d_start, h4_start, h1_start, now_epoch)
        if self.price_series == "real":
            all_sets = (("1D", daily), ("4H", bars4), ("1H", bars1))
            missing_raw = [
                f"{tf}:{c.open_time}" for tf, bars in all_sets for c in bars
                if any(v is None for v in (c.real_open, c.real_high, c.real_low, c.real_close))
            ]
            if missing_raw:
                # Do not mix adjusted OHLC and raw `real*` OHLC inside one strategy series.
                # A warning alone would be unsafe because the backtester might still consume
                # the mixed bars and report apparently valid performance.
                raise MexcError(
                    f"Configured real OHLC series is incomplete on {len(missing_raw)} candle(s); "
                    "select a reviewed consistent series or resolve the MEXC payload semantics first."
                )
            def use_real(c: Candle) -> Candle:
                return replace(c, open=float(c.real_open), high=float(c.real_high),
                               low=float(c.real_low), close=float(c.real_close))
            daily = [use_real(c) for c in daily]
            bars4 = [use_real(c) for c in bars4]
            bars1 = [use_real(c) for c in bars1]
        h12 = aggregate_12h_from_4h(bars4)
        if not h12:
            warnings.append("No complete UTC-aligned 12H bars could be built from the retrieved 4H bars.")
        if len(daily) < 200:
            warnings.append(f"Only {len(daily)} completed 1D candles available; EMA200 warm-up is incomplete.")
        if not bars1:
            warnings.append("No completed 1H candles returned.")
        for tf, bars, step in (("1H", bars1, 3600), ("4H", bars4, 4 * 3600), ("1D", daily, 86400)):
            if len(bars) > 1:
                gaps = sum(1 for a, b in zip(bars, bars[1:]) if b.open_time - a.open_time != step)
                if gaps:
                    warnings.append(f"Detected {gaps} gap(s)/irregular interval(s) in retrieved {tf} history.")
        contract = None
        try:
            contract = await self.client.contract_detail(symbol)
            if not contract:
                warnings.append("Contract was not found in current contract metadata.")
            else:
                state = contract.get("state")
                api_allowed = contract.get("apiAllowed")
                if state not in (0, "0"):
                    warnings.append(f"Contract state is not enabled (state={state!r}).")
                if api_allowed not in (True, 1, "1", "true", "True"):
                    warnings.append("Contract API eligibility is not explicitly true.")
        except MexcError as exc:
            warnings.append(f"Contract metadata fetch failed: {str(exc)[:180]}")
        return SymbolData(symbol, {"1D": daily, "12H": h12, "4H": bars4, "1H": bars1}, contract, tuple(warnings))

    async def _fetch_parallel(self, symbol: str, d_start: int, h4_start: int, h1_start: int, now: int):
        import asyncio

        d_task = self.client.get_klines_window(symbol, "1D", start=d_start, end=now,
            now_epoch=now, close_grace_seconds=self.close_grace_seconds)
        h4_task = self.client.get_klines_window(symbol, "4H", start=h4_start, end=now,
            now_epoch=now, close_grace_seconds=self.close_grace_seconds)
        h1_task = self.client.get_klines_window(symbol, "1H", start=h1_start, end=now,
            now_epoch=now, close_grace_seconds=self.close_grace_seconds)
        results = await asyncio.gather(d_task, h4_task, h1_task, return_exceptions=True)
        names = ("1D", "4H", "1H")
        output: list[list[Candle]] = []
        for tf, result in zip(names, results):
            if isinstance(result, Exception):
                raise MexcError(f"Failed to fetch {tf} candles for {symbol}: {type(result).__name__}") from result
            output.append(result)
        return output[0], output[1], output[2]
