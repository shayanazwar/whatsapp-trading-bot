from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Mapping, Any


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

    if not all(isfinite(v) for v in (high, low)) or timestamp <= 0:
        return None
    return timestamp, high, low


def simulate_trade(
    signal: Mapping[str, Any],
    future_candles: Iterable[Any],
    *,
    signal_close_time_ms: int,
) -> SimulatedTrade | None:
    """Simulate one engine signal using subsequent closed 5M candles.

    Entry is assumed filled at the engine's reported entry immediately after
    the signal candle closes. SL/TP levels remain fixed for the simulation.

    Conservative same-candle rule: when SL and either TP1 or TP2 are both
    touched by the same OHLC candle, SL is treated as occurring first because
    intrabar order is unknowable from OHLC data alone.
    """

    symbol = str(signal.get("symbol") or "UNKNOWN")
    side = str(signal.get("setup") or signal.get("side") or "").upper()
    entry = _number(signal.get("entry"))
    stop = _number(signal.get("stop_loss"))
    tp1 = _number(signal.get("tp1"))
    tp2 = _number(signal.get("tp2"))

    if side not in {"LONG", "SHORT"}:
        return None
    if None in (entry, stop, tp1, tp2):
        return None
    assert entry is not None and stop is not None and tp1 is not None and tp2 is not None

    risk = abs(entry - stop)
    reward = abs(tp2 - entry)
    if risk <= 0 or reward <= 0:
        return None

    if side == "LONG":
        if not (stop < entry < tp1 < tp2):
            return None
    else:
        if not (stop > entry > tp1 > tp2):
            return None

    planned_rr = reward / risk
    tp1_hit = False

    for candle in future_candles:
        parsed = _candle_values(candle)
        if parsed is None:
            continue
        timestamp, high, low = parsed
        if timestamp + 300_000 <= signal_close_time_ms:
            continue

        if side == "LONG":
            sl_touched = low <= stop
            tp2_touched = high >= tp2
            tp1_touched = high >= tp1
        else:
            sl_touched = high >= stop
            tp2_touched = low <= tp2
            tp1_touched = low <= tp1

        # Conservative rule for any candle that touches both the stop and
        # a profit target. OHLC data cannot reveal the intrabar sequence,
        # so the stop is treated as occurring first.
        if sl_touched and (tp1_touched or tp2_touched):
            return SimulatedTrade(
                symbol=symbol,
                side=side,
                signal_time_ms=signal_close_time_ms,
                entry=entry,
                stop_loss=stop,
                tp1=tp1,
                tp2=tp2,
                planned_rr=planned_rr,
                tp1_hit=tp1_hit,
                tp2_hit=False,
                sl_hit=True,
                outcome="SL",
                r_multiple=-1.0,
                exit_time_ms=timestamp + 300_000,
            )

        if tp2_touched:
            return SimulatedTrade(
                symbol=symbol,
                side=side,
                signal_time_ms=signal_close_time_ms,
                entry=entry,
                stop_loss=stop,
                tp1=tp1,
                tp2=tp2,
                planned_rr=planned_rr,
                tp1_hit=True,
                tp2_hit=True,
                sl_hit=False,
                outcome="TP2",
                r_multiple=planned_rr,
                exit_time_ms=timestamp + 300_000,
            )

        if tp1_touched:
            tp1_hit = True

        if sl_touched:
            return SimulatedTrade(
                symbol=symbol,
                side=side,
                signal_time_ms=signal_close_time_ms,
                entry=entry,
                stop_loss=stop,
                tp1=tp1,
                tp2=tp2,
                planned_rr=planned_rr,
                tp1_hit=tp1_hit,
                tp2_hit=False,
                sl_hit=True,
                outcome="SL",
                r_multiple=-1.0,
                exit_time_ms=timestamp + 300_000,
            )

    return SimulatedTrade(
        symbol=symbol,
        side=side,
        signal_time_ms=signal_close_time_ms,
        entry=entry,
        stop_loss=stop,
        tp1=tp1,
        tp2=tp2,
        planned_rr=planned_rr,
        tp1_hit=tp1_hit,
        tp2_hit=False,
        sl_hit=False,
        outcome="OPEN",
        r_multiple=None,
        exit_time_ms=None,
    )
