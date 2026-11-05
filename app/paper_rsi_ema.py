"""Safety-first simulated execution for the 3-minute RSI/EMA strategy.

This module never calls a private exchange endpoint. It models fills from public ticker/OHLC data
and is enabled only by PAPER_TRADING_ENABLED=true. Live order execution is intentionally absent.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.market.candles import Candle


@dataclass(frozen=True, slots=True)
class PaperConfig:
    equity_start: float = 100.0
    margin_usdt: float = 1.0
    max_leverage: int = 50
    target_net_usdt: float = 0.30
    requested_stop_usdt: float = 3.0
    taker_fee_rate: float = 0.0008
    slippage_bps: float = 5.0
    default_maintenance_margin_rate: float = 0.005

    def validate(self) -> None:
        if self.equity_start <= 0 or self.margin_usdt <= 0:
            raise ValueError("Paper equity and margin must be positive")
        if not 1 <= self.max_leverage <= 50:
            raise ValueError("Paper leverage cap must be from 1 to 50")
        if self.target_net_usdt <= 0 or self.requested_stop_usdt <= 0:
            raise ValueError("Paper TP and requested stop must be positive")
        if not 0 <= self.taker_fee_rate < 0.1:
            raise ValueError("Invalid simulated taker fee")
        if not 0 <= self.slippage_bps <= 1000:
            raise ValueError("Invalid simulated slippage")
        if not 0 < self.default_maintenance_margin_rate < 0.5:
            raise ValueError("Invalid maintenance-margin assumption")


def _number(source: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        try:
            value = float(source.get(key))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            return value
    return default


def _round_down(value: float, step: float) -> float:
    return math.floor((value + 1e-12) / step) * step


def _contract_leverage(contract: Mapping[str, Any]) -> float:
    for key in ("countryConfigContractMaxLeverage", "maxLeverage"):
        value = _number(contract, key, default=0.0)
        if value > 0:
            return value
    return 0.0


def create_paper_position(
    *, symbol: str, signal: Mapping[str, Any], ticker: Mapping[str, Any],
    contract: Mapping[str, Any], entry_time: int, equity: float,
    config: PaperConfig | None = None,
) -> dict[str, Any]:
    """Size and price a single simulated LONG or raise ValueError when not executable."""
    cfg = config or PaperConfig()
    cfg.validate()
    if str(signal.get("direction", "")).upper() != "LONG":
        raise ValueError("This selected strategy permits LONG entries only")
    if equity < cfg.margin_usdt:
        raise ValueError("Simulated equity is below required initial margin")
    reference_price = _number(ticker, "lastPrice", "last_price", default=0.0)
    if reference_price <= 0:
        raise ValueError("Fresh ticker has no positive last price")
    contract_size = _number(contract, "contractSize", "contract_size", default=0.0)
    volume_step = _number(contract, "volUnit", "volumeUnit", "quantityStep", default=0.0)
    min_volume = _number(contract, "minVol", "minVolume", "minOrderQty", default=volume_step)
    max_volume = _number(contract, "maxVol", "maxVolume", "maxOrderQty", default=float("inf"))
    if contract_size <= 0 or volume_step <= 0 or min_volume < 0 or max_volume <= 0:
        raise ValueError("Contract size or quantity limits are missing/invalid")
    contract_leverage = _contract_leverage(contract)
    if contract_leverage <= 0:
        raise ValueError("Contract leverage limit is unavailable; refusing paper entry")
    leverage = int(max(1, min(cfg.max_leverage, contract_leverage)))
    slippage = cfg.slippage_bps / 10000.0
    entry_fill = reference_price * (1.0 + slippage)
    target_notional = cfg.margin_usdt * leverage
    raw_qty = target_notional / (entry_fill * contract_size)
    quantity = _round_down(raw_qty, volume_step)
    precision = max(0, min(12, len(f"{volume_step:.12f}".rstrip("0").split(".")[-1])))
    quantity = round(quantity, precision)
    if quantity <= 0 or quantity < min_volume or quantity > max_volume:
        raise ValueError("Required position size violates contract quantity limits")
    actual_notional = quantity * entry_fill * contract_size
    actual_margin = actual_notional / leverage
    if actual_margin > cfg.margin_usdt * 1.000001:
        raise ValueError("Rounded position would exceed the approved margin allocation")
    entry_fee = actual_notional * cfg.taker_fee_rate
    if equity < actual_margin + entry_fee:
        raise ValueError("Simulated equity cannot cover margin and entry fee")
    maintenance_rate = _number(contract, "maintenanceMarginRate", "maintenance_margin_rate",
                               default=cfg.default_maintenance_margin_rate)
    if not 0 < maintenance_rate < 0.5:
        maintenance_rate = cfg.default_maintenance_margin_rate
    # This simplified threshold is deliberately conservative and is not an exchange liquidation quote.
    liquidation_price = entry_fill * max(0.01, 1.0 - (1.0 / leverage - maintenance_rate))
    stop_price = entry_fill - cfg.requested_stop_usdt / max(quantity * contract_size, 1e-18)
    # Solve for the trigger price that targets the desired net profit after estimated fees/slippage.
    needed_exit_reference = (
        cfg.target_net_usdt / max(quantity * contract_size, 1e-18)
        + entry_fill * (1.0 + cfg.taker_fee_rate)
    ) / max(1.0 - cfg.taker_fee_rate, 1e-9)
    target_price = needed_exit_reference / max(1.0 - slippage, 1e-9)
    position_id = str(uuid.uuid4())
    meta = {
        "model": "RSI-EMA-3M-LONG-v1", "simulated": True, "actual_live_order": False,
        "entry_reference_price": reference_price, "entry_fill_price": entry_fill,
        "contract_size": contract_size, "quantity_step": volume_step, "leverage": leverage,
        "initial_margin": actual_margin, "notional": actual_notional,
        "entry_fee_estimate": entry_fee, "taker_fee_rate": cfg.taker_fee_rate,
        "slippage_bps_per_side": cfg.slippage_bps,
        "liquidation_price_estimate": liquidation_price,
        "requested_stop_loss_usdt": cfg.requested_stop_usdt,
        "net_target_usdt": cfg.target_net_usdt,
        "maintenance_margin_rate_assumption": maintenance_rate,
        "signal": dict(signal), "assumptions": asdict(cfg),
    }
    return {
        "id": position_id, "symbol": symbol, "side": "LONG", "entry_time": int(entry_time),
        "entry_price": entry_fill, "stop_price": stop_price, "target_price": target_price,
        "quantity": quantity, "mode": "rsi_ema_paper", "meta": meta,
    }


def resolve_paper_exit(
    position: Mapping[str, Any], new_bars: Sequence[Candle],
    funding_events: Sequence[Mapping[str, Any]] | None = (),
) -> dict[str, Any] | None:
    """Return the first conservative OHLC exit, or None while the simulated position remains open.

    ``funding_events=None`` means the funding query failed; in that case stop/liquidation exits
    remain active, but a net-profit take-profit is held until funding can be verified.
    """
    meta = dict(position.get("meta") or {})
    if not meta.get("simulated") or position.get("mode") != "rsi_ema_paper":
        return None
    entry_time = int(position["entry_time"])
    ref_entry = _number(meta, "entry_reference_price", default=float(position["entry_price"]))
    entry_fill = float(position["entry_price"])
    quantity = float(position["quantity"])
    contract_size = _number(meta, "contract_size", default=0.0)
    fee_rate = _number(meta, "taker_fee_rate", default=0.0008)
    slip = _number(meta, "slippage_bps_per_side", default=5.0) / 10000.0
    liquidation_price = _number(meta, "liquidation_price_estimate", default=0.0)
    margin = _number(meta, "initial_margin", default=1.0)
    notional = _number(meta, "notional", default=entry_fill * quantity * contract_size)

    def funding_cost_until(exit_time: int) -> float:
        total = 0.0
        if funding_events is None:
            return 0.0
        for event in funding_events:
            stamp_raw = event.get("settleTime", event.get("fundingTime", event.get("timestamp", event.get("time", 0))))
            try:
                stamp = int(float(stamp_raw))
            except (TypeError, ValueError, OverflowError):
                continue
            if stamp > 100_000_000_000:
                stamp //= 1000
            event_symbol = str(event.get("symbol", position["symbol"])).upper().replace("-", "_")
            if event_symbol != str(position["symbol"]).upper().replace("-", "_") or not entry_time < stamp <= exit_time:
                continue
            rate = _number(event, "fundingRate", "funding_rate", "rate", default=0.0)
            total += notional * rate  # positive funding rate is a LONG cost
        return total

    for bar in sorted(new_bars, key=lambda c: c.open_time):
        if not bar.complete or bar.timeframe != "3M" or bar.symbol != str(position["symbol"]):
            continue
        if bar.open_time < entry_time:
            continue
        reason = None
        exit_ref = 0.0
        if liquidation_price > 0 and bar.low <= liquidation_price:
            reason, exit_ref = "LIQUIDATION_ESTIMATE", liquidation_price
        elif bar.low <= float(position["stop_price"]):
            reason, exit_ref = "STOP_LOSS", float(position["stop_price"])
        elif funding_events is not None:
            paid_funding = funding_cost_until(bar.close_time)
            target_price = float(position["target_price"])
            if paid_funding:
                target_price += paid_funding / max(quantity * contract_size * (1.0 - fee_rate) * (1.0 - slip), 1e-18)
            if bar.high >= target_price:
                reason, exit_ref = "TAKE_PROFIT_NET_TARGET", target_price
        if reason is None:
            continue
        exit_fill = exit_ref * (1.0 - slip)
        gross = quantity * contract_size * (exit_ref - ref_entry)
        entry_fee = _number(meta, "entry_fee_estimate", default=entry_fill * quantity * contract_size * fee_rate)
        exit_fee = quantity * contract_size * exit_fill * fee_rate
        slippage_cost = quantity * contract_size * max(0.0, entry_fill - ref_entry) + quantity * contract_size * max(0.0, exit_ref - exit_fill)
        funding_cost = funding_cost_until(bar.close_time)
        net = gross - entry_fee - exit_fee - slippage_cost - funding_cost
        liquidation_adjustment = 0.0
        if reason == "LIQUIDATION_ESTIMATE":
            liquidation_adjustment = max(0.0, net + margin)
            net -= liquidation_adjustment
        return {
            "exit_time": bar.close_time, "exit_reference_price": exit_ref,
            "exit_fill_price": exit_fill, "net_pnl": net, "gross_pnl": gross,
            "fees": entry_fee + exit_fee, "estimated_slippage_cost": slippage_cost,
            "funding_cost": funding_cost if funding_events is not None else None,
            "funding_data_verified": funding_events is not None,
            "liquidation_adjustment": liquidation_adjustment,
            "reason": reason, "bar_time": bar.open_time,
        }
    return None
