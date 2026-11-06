from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Side = Literal["LONG", "SHORT"]
Module = Literal["SWEEP", "DONCHIAN", "SWEEP+DONCHIAN"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Contract:
    symbol: str
    base_coin: str
    quote_coin: str
    contract_size: float
    price_tick: float
    quantity_step: float
    min_volume: float
    taker_fee_rate: float
    max_leverage: int
    quote_volume_24h: float = 0.0
    state: int = 0
    is_new: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any], quote_volume_24h: float = 0.0) -> "Contract":
        return cls(
            symbol=str(raw.get("symbol", "")),
            base_coin=str(raw.get("baseCoin", "")),
            quote_coin=str(raw.get("quoteCoin", "")),
            contract_size=float(raw.get("contractSize") or 0),
            price_tick=float(raw.get("priceUnit") or 0),
            quantity_step=float(raw.get("volUnit") or 1),
            min_volume=float(raw.get("minVol") or 1),
            taker_fee_rate=float(raw.get("takerFeeRate") or 0),
            max_leverage=int(raw.get("countryConfigContractMaxLeverage") or raw.get("maxLeverage") or 1),
            quote_volume_24h=quote_volume_24h,
            state=int(raw.get("state", 0)),
            is_new=bool(raw.get("isNew", False)),
        )


@dataclass
class TradeSignal:
    symbol: str
    side: Side
    module: Module
    signal_time: datetime
    reference_entry: float
    stop_loss: float
    take_profit: float
    risk_amount_usd: float
    risk_pct: float
    contracts: float
    leverage: float
    estimated_fees_usd: float
    estimated_slippage_usd: float
    estimated_funding_usd: float
    gross_rr: float
    net_rr: float
    btc_bias: str
    relative_strength_score: float
    funding_rate: float
    status: str = "CONFIRMED"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["signal_time"] = self.signal_time.isoformat()
        return result
