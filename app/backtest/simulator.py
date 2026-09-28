from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Mapping, Any


M5_MS = 300_000
DEFAULT_FEE_RATE = 0.0004       # 4 bps per side
DEFAULT_SLIPPAGE_BPS = 2.0      # 2 bps adverse slippage per fill
DEFAULT_MAX_HOLDING_MINUTES = 360


@dataclass(frozen=True)
class SimulatedTrade:
    symbol: str
    side: str
    signal_time_ms: int
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    planned_rr: float
    tp1_hit: bool
    tp2_hit: bool
    sl_hit: bool
    outcome: str
    r_multiple: float | None
    exit_time_ms: int | None
    hold_minutes: float | None = None
    fees_r: float = 0.0
    slippage_r: float = 0.0
    entry_execution: float | None = None
    exit_execution: float | None = None
    expired: bool = False
    regime: str | None = None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _candle_values(candle: Any) -> tuple[int, float, float] | None:
    try:
        if isinstance(candle, Mapping):
            timestamp = int(float(candle.get("time", candle.get("timestamp"))))
            high = float(candle["high"])
            low = float(candle["low"])
        else:
            timestamp = int(float(candle[0]))
            high = float(candle[2])
            low = float(candle[3])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if not all(isfinite(value) for value in (high, low)) or timestamp <= 0 or low > high:
        return None
    return timestamp, high, low


def _adverse_slippage(price: float, side: str, *, is_entry: bool, slippage_bps: float) -> float:
    slip = max(0.0, float(slippage_bps)) / 10_000.0
    if side == "LONG":
        return price * (1.0 + slip) if is_entry else price * (1.0 - slip)
    return price * (1.0 - slip) if is_entry else price * (1.0 + slip)


def _hold_minutes(signal_time_ms: int, timestamp_ms: int) -> float:
    return max(0.0, (timestamp_ms - signal_time_ms) / 60_000.0)


def simulate_trade(
    signal: Mapping[str, Any],
    future_candles: Iterable[Any],
    *,
    signal_close_time_ms: int,
    fee_rate: float = DEFAULT_FEE_RATE,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    max_holding_minutes: float | None = None,
) -> SimulatedTrade | None:
    """Simulate intraday execution with adverse slippage, fees and expiry.

    Structural SL/TP are never translated. The executable entry is modeled
    separately and actual R is calculated from executable entry/exit prices.
    A candle that touches both sides resolves conservatively as SL first.
    """
    symbol = str(signal.get("symbol") or "UNKNOWN")
    side = str(signal.get("setup") or signal.get("side") or "").upper()
    entry = _number(signal.get("entry")); stop = _number(signal.get("stop_loss")); tp1 = _number(signal.get("tp1")); tp2 = _number(signal.get("tp2"))
    if side not in {"LONG", "SHORT"} or None in (entry, stop, tp1, tp2):
        return None
    assert entry is not None and stop is not None and tp1 is not None and tp2 is not None

    risk_planned = abs(entry - stop)
    if risk_planned <= 0:
        return None
    if side == "LONG" and not (stop < entry < tp1 < tp2):
        return None
    if side == "SHORT" and not (tp2 < tp1 < entry < stop):
        return None

    max_hold = float(max_holding_minutes if max_holding_minutes is not None else signal.get("intraday_max_hold_minutes") or DEFAULT_MAX_HOLDING_MINUTES)
    if max_hold <= 0:
        max_hold = DEFAULT_MAX_HOLDING_MINUTES
    fee_rate = max(0.0, float(fee_rate))
    slippage_bps = max(0.0, float(slippage_bps))

    entry_exec = _adverse_slippage(entry, side, is_entry=True, slippage_bps=slippage_bps)
    if side == "LONG" and entry_exec <= stop:
        return None
    if side == "SHORT" and entry_exec >= stop:
        return None
    risk_exec = abs(entry_exec - stop)
    if risk_exec <= 0:
        return None

    planned_rr = abs(tp2 - entry) / risk_planned
    regime = str(signal.get("regime") or signal.get("trend_4h") or "UNKNOWN")
    tp1_hit = False
    last_valid_close = signal_close_time_ms

    def make_trade(outcome: str, ts: int | None, *, tp2_hit: bool = False, sl_hit: bool = False, exit_price: float | None = None, expired: bool = False) -> SimulatedTrade:
        hold = None if ts is None else _hold_minutes(signal_close_time_ms, ts)
        exit_exec = None
        r_value = None
        fees_r = 0.0
        slippage_r = 0.0
        if exit_price is not None:
            exit_exec = _adverse_slippage(exit_price, side, is_entry=False, slippage_bps=slippage_bps)
            reward_or_loss = (exit_exec - entry_exec) if side == "LONG" else (entry_exec - exit_exec)
            gross_r = reward_or_loss / risk_exec
            notional = abs(entry_exec) + abs(exit_exec)
            fees_r = notional * fee_rate / risk_exec
            r_value = gross_r - fees_r
            slippage_r = (abs(entry_exec - entry) + abs(exit_exec - exit_price)) / risk_exec
        return SimulatedTrade(
            symbol=symbol,
            side=side,
            signal_time_ms=int(signal_close_time_ms),
            entry=entry,
            stop_loss=stop,
            tp1=tp1,
            tp2=tp2,
            planned_rr=planned_rr,
            tp1_hit=tp1_hit,
            tp2_hit=tp2_hit,
            sl_hit=sl_hit,
            outcome=outcome,
            r_multiple=r_value,
            exit_time_ms=ts,
            hold_minutes=hold,
            fees_r=fees_r,
            slippage_r=slippage_r,
            entry_execution=entry_exec,
            exit_execution=exit_exec,
            expired=expired,
            regime=regime,
        )

    for candle in future_candles:
        parsed = _candle_values(candle)
        if parsed is None:
            continue
        timestamp, high, low = parsed
        close_time = timestamp + M5_MS
        if close_time <= signal_close_time_ms:
            continue
        last_valid_close = close_time

        if _hold_minutes(signal_close_time_ms, close_time) > max_hold:
            expiry_ts = int(signal_close_time_ms + max_hold * 60_000)
            return make_trade("EXPIRED", expiry_ts, expired=True)

        if side == "LONG":
            sl_touched = low <= stop
            tp2_touched = high >= tp2
            tp1_touched = high >= tp1
        else:
            sl_touched = high >= stop
            tp2_touched = low <= tp2
            tp1_touched = low <= tp1

        # Without intrabar ordering data, a same-candle collision resolves SL first.
        if sl_touched and (tp1_touched or tp2_touched):
            return make_trade("SL", close_time, sl_hit=True, exit_price=stop)
        if tp2_touched:
            tp1_hit = True
            return make_trade("TP2", close_time, tp2_hit=True, exit_price=tp2)
        if tp1_touched:
            tp1_hit = True
        if sl_touched:
            return make_trade("SL", close_time, sl_hit=True, exit_price=stop)

    return make_trade("EXPIRED", last_valid_close if last_valid_close > signal_close_time_ms else None, expired=True)
