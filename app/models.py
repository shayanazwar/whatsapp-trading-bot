from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True, slots=True)
class Candle:
    time: int  # UTC Unix seconds; candle open time
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def is_valid(self) -> bool:
        values = (self.open, self.high, self.low, self.close, self.volume)
        return all(isinstance(v, (int, float)) and v == v and abs(v) != float("inf") for v in values) and (
            self.time >= 0 and self.low > 0 and self.high >= max(self.open, self.close)
            and self.low <= min(self.open, self.close) and self.volume >= 0
        )


@dataclass(frozen=True, slots=True)
class ContractInfo:
    symbol: str
    contract_size: float
    price_tick: float
    volume_step: float
    min_volume: float
    max_volume: float
    max_leverage: float
    country_max_leverage: float
    taker_fee_rate: float
    maintenance_margin_rate: float
    liquidation_fee_rate: float


@dataclass(frozen=True, slots=True)
class Signal:
    direction: Direction
    candle_time: int
    price: float
    rsi: float
    tp_price: float
    tp_pct: float
    rsi_period: int
    rsi_upper: float
    rsi_lower: float
