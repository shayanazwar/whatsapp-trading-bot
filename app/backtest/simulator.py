from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Iterable, Mapping


M5_MS = 300_000
DEFAULT_FEE_RATE = 0.0004
DEFAULT_SLIPPAGE_BPS = 2.0
DEFAULT_MAX_HOLDING_MINUTES = 360
DEFAULT_SAME_BAR_RULE = "SL_FIRST"
VALID_SAME_BAR_RULES = {"SL_FIRST", "TP_FIRST"}


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
    quality: Mapping[str, float] = field(default_factory=dict)

    # Explicit multi-stage execution/state accounting.
    initial_position_size: float = 1.0
    tp1_close_size: float = 0.0
    final_close_size: float = 0.0
    remaining_position_size: float = 0.0
    breakeven_hit: bool = False
    original_stop_loss: float | None = None
    breakeven_stop: float | None = None
    tp1_execution: float | None = None
    breakeven_execution: float | None = None
    final_exit_execution: float | None = None
    realized_pnl: float = 0.0
    gross_pnl: float = 0.0
    entry_fee: float = 0.0
    exit_fees: float = 0.0
    position_contract_size: float = 1.0
    same_bar_rule: str = DEFAULT_SAME_BAR_RULE
    state: str = "FINALIZED"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _candle_values(candle: Any) -> tuple[int, float, float, float, float] | None:
    try:
        if isinstance(candle, Mapping):
            raw_time = candle.get("time", candle.get("timestamp", candle.get("openTime")))
            timestamp = int(float(raw_time))
            open_price = float(candle["open"])
            high = float(candle["high"])
            low = float(candle["low"])
            close = float(candle["close"])
        else:
            timestamp = int(float(candle[0]))
            open_price = float(candle[1])
            high = float(candle[2])
            low = float(candle[3])
            close = float(candle[4])
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        return None
    if timestamp <= 0:
        return None
    if not all(isfinite(value) for value in (open_price, high, low, close)):
        return None
    if low > high or close < low or close > high:
        return None
    return timestamp, open_price, high, low, close


def _adverse_slippage(
    price: float,
    side: str,
    *,
    is_entry: bool,
    slippage_bps: float,
) -> float:
    slip = max(0.0, float(slippage_bps)) / 10_000.0
    if side == "LONG":
        return price * (1.0 + slip) if is_entry else price * (1.0 - slip)
    return price * (1.0 - slip) if is_entry else price * (1.0 + slip)


def _hold_minutes(signal_time_ms: int, timestamp_ms: int) -> float:
    return max(0.0, (timestamp_ms - signal_time_ms) / 60_000.0)


def _same_bar_rule(value: Any) -> str:
    rule = str(value or DEFAULT_SAME_BAR_RULE).upper().strip()
    return rule if rule in VALID_SAME_BAR_RULES else DEFAULT_SAME_BAR_RULE


def _position_size(signal: Mapping[str, Any]) -> tuple[float, float] | None:
    raw_size = signal.get("position_size", signal.get("quantity", 1.0))
    raw_contract = signal.get("contract_size", 1.0)
    raw_step = signal.get("position_step", signal.get("vol_unit", 0.0))

    size = _number(raw_size)
    contract_size = _number(raw_contract)
    step = _number(raw_step)
    if size is None or contract_size is None or size <= 0 or contract_size <= 0:
        return None
    if step is None or step < 0:
        return None

    if step > 0:
        try:
            size_d = Decimal(str(size))
            step_d = Decimal(str(step))
            half_d = size_d / Decimal("2")
            # The exchange must represent both the initial size and the exact
            # 50% partial exit. No rounding-up or negative residuals are allowed.
            if (size_d / step_d) != (size_d / step_d).to_integral_value():
                return None
            if (half_d / step_d) != (half_d / step_d).to_integral_value():
                return None
        except (InvalidOperation, ValueError, OverflowError):
            return None

    return size, contract_size


def _quality_snapshot(signal: Mapping[str, Any]) -> dict[str, float]:
    snapshot: dict[str, float] = {}
    for key in (
        "score", "confirmation_family_count", "bos_15m_strength",
        "trigger_quality_15m", "trigger_quality_5m", "retest",
        "rvol_15m", "rvol_5m", "rsi", "rsi_5m",
        "adx_4h", "atr_percentile", "sl_atr", "stop_distance_pct",
        "ema_extension_atr", "one_hour_long_votes", "one_hour_short_votes",
        "macd_hist_delta",
    ):
        if key == "retest":
            nested = signal.get("retest") or {}
            value = nested.get("quality") if isinstance(nested, Mapping) else None
            if value is not None:
                try:
                    snapshot["retest_quality"] = float(value)
                except (TypeError, ValueError):
                    pass
            continue
        value = signal.get(key)
        if value is None:
            continue
        try:
            snapshot[key] = float(value)
        except (TypeError, ValueError):
            continue
    return snapshot


def simulate_trade(
    signal: Mapping[str, Any],
    future_candles: Iterable[Any],
    *,
    signal_close_time_ms: int,
    fee_rate: float = DEFAULT_FEE_RATE,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    max_holding_minutes: float | None = None,
    same_bar_rule: str | None = None,
) -> SimulatedTrade | None:
    """Simulate a deterministic TP1 -> breakeven -> TP2 state machine.

    State transitions:
      OPEN -> TP1_PARTIAL -> TP2_FINAL
      OPEN -> TP1_PARTIAL -> BREAKEVEN
      OPEN -> STOP_FINAL
      OPEN -> EXPIRED

    TP1 always closes exactly 50% of the initial position. Once TP1 has
    executed, the original SL is permanently disabled and the remaining 50%
    uses a breakeven stop at the executable entry price. TP2 or breakeven
    closes the full residual position.

    Same-bar ambiguity is configurable. ``SL_FIRST`` is the default and is
    deliberately pessimistic for competing active exits; ``TP_FIRST`` is
    available for sensitivity analysis.
    """
    try:
        symbol = str(signal.get("symbol") or "UNKNOWN")
        side = str(signal.get("setup") or signal.get("side") or "").upper()
        entry = _number(signal.get("entry"))
        stop = _number(signal.get("stop_loss"))
        tp1 = _number(signal.get("tp1"))
        tp2 = _number(signal.get("tp2"))
        signal_time = int(signal_close_time_ms)
    except (TypeError, ValueError, OverflowError, AttributeError):
        return None

    if side not in {"LONG", "SHORT"} or any(value is None for value in (entry, stop, tp1, tp2)):
        return None
    if entry is None or stop is None or tp1 is None or tp2 is None:
        return None

    planned_risk = abs(entry - stop)
    if planned_risk <= 0 or not isfinite(planned_risk):
        return None
    if side == "LONG" and not (stop < entry < tp1 < tp2):
        return None
    if side == "SHORT" and not (tp2 < tp1 < entry < stop):
        return None

    size_data = _position_size(signal)
    if size_data is None:
        return None
    initial_size, contract_size = size_data
    half_size = initial_size / 2.0
    if half_size <= 0 or not isfinite(half_size):
        return None

    try:
        max_hold = float(
            max_holding_minutes
            if max_holding_minutes is not None
            else signal.get("intraday_max_hold_minutes") or DEFAULT_MAX_HOLDING_MINUTES
        )
        fee_rate = max(0.0, float(fee_rate))
        slippage_bps = max(0.0, float(slippage_bps))
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(max_hold) or max_hold <= 0:
        max_hold = DEFAULT_MAX_HOLDING_MINUTES
    if not isfinite(fee_rate) or not isfinite(slippage_bps):
        return None

    rule = _same_bar_rule(
        same_bar_rule if same_bar_rule is not None else signal.get("same_bar_rule")
    )

    entry_exec = _adverse_slippage(entry, side, is_entry=True, slippage_bps=slippage_bps)
    if not isfinite(entry_exec) or entry_exec <= 0:
        return None
    if side == "LONG" and not (stop < entry_exec < tp1 < tp2):
        return None
    if side == "SHORT" and not (tp2 < tp1 < entry_exec < stop):
        return None

    risk_exec_per_unit = abs(entry_exec - stop)
    initial_risk_cash = risk_exec_per_unit * initial_size * contract_size
    if risk_exec_per_unit <= 0 or initial_risk_cash <= 0 or not isfinite(initial_risk_cash):
        return None

    entry_fee = abs(entry_exec) * initial_size * contract_size * fee_rate
    if not isfinite(entry_fee):
        return None

    realized_pnl = -entry_fee
    gross_pnl = 0.0
    exit_fees = 0.0
    slippage_cash = abs(entry_exec - entry) * initial_size * contract_size

    tp1_hit = False
    tp2_hit = False
    sl_hit = False
    breakeven_hit = False
    tp1_exec: float | None = None
    be_exec: float | None = None
    final_exec: float | None = None
    final_ts: int | None = None
    final_fill_size = 0.0
    remaining_size = initial_size
    state = "OPEN"

    previous_close: float | None = None
    previous_close_time: int | None = None

    def execute_exit(base_price: float, quantity: float) -> float | None:
        nonlocal realized_pnl, gross_pnl, exit_fees, slippage_cash
        if quantity <= 0 or quantity > remaining_size + 1e-12:
            return None
        if not isfinite(base_price) or base_price <= 0:
            return None
        exit_exec = _adverse_slippage(
            base_price,
            side,
            is_entry=False,
            slippage_bps=slippage_bps,
        )
        if not isfinite(exit_exec) or exit_exec <= 0:
            return None
        pnl_per_unit = exit_exec - entry_exec if side == "LONG" else entry_exec - exit_exec
        gross = pnl_per_unit * quantity * contract_size
        fee = abs(exit_exec) * quantity * contract_size * fee_rate
        slip_cash = abs(exit_exec - base_price) * quantity * contract_size
        if not all(isfinite(value) for value in (gross, fee, slip_cash)):
            return None
        gross_pnl += gross
        exit_fees += fee
        realized_pnl += gross - fee
        slippage_cash += slip_cash
        return exit_exec

    def build_trade(
        outcome: str,
        ts: int | None,
        *,
        expired: bool = False,
    ) -> SimulatedTrade | None:
        if abs(remaining_size) > 1e-9:
            return None
        final_ts = ts
        hold = None if ts is None else _hold_minutes(signal_time, ts)
        net_r = realized_pnl / initial_risk_cash if initial_risk_cash > 0 else None
        fees_r = (entry_fee + exit_fees) / initial_risk_cash if initial_risk_cash > 0 else 0.0
        slippage_r = slippage_cash / initial_risk_cash if initial_risk_cash > 0 else 0.0
        if net_r is not None and not isfinite(net_r):
            net_r = None
        return SimulatedTrade(
            symbol=symbol,
            side=side,
            signal_time_ms=signal_time,
            entry=entry,
            stop_loss=stop,
            tp1=tp1,
            tp2=tp2,
            planned_rr=abs(tp2 - entry) / planned_risk,
            tp1_hit=tp1_hit,
            tp2_hit=tp2_hit,
            sl_hit=sl_hit,
            outcome=outcome,
            r_multiple=net_r,
            exit_time_ms=final_ts,
            hold_minutes=hold,
            fees_r=fees_r,
            slippage_r=slippage_r,
            entry_execution=entry_exec,
            exit_execution=final_exec,
            expired=expired,
            regime=str(signal.get("regime") or signal.get("trend_4h") or "UNKNOWN"),
            quality=_quality_snapshot(signal),
            initial_position_size=initial_size,
            tp1_close_size=half_size if tp1_hit else 0.0,
            final_close_size=final_fill_size,
            remaining_position_size=0.0,
            breakeven_hit=breakeven_hit,
            original_stop_loss=stop,
            breakeven_stop=entry_exec if tp1_hit else None,
            tp1_execution=tp1_exec,
            breakeven_execution=be_exec,
            final_exit_execution=final_exec,
            realized_pnl=realized_pnl,
            gross_pnl=gross_pnl,
            entry_fee=entry_fee,
            exit_fees=exit_fees,
            position_contract_size=contract_size,
            same_bar_rule=rule,
            state="FINALIZED",
        )

    def close_final(
        outcome: str,
        ts: int,
        base_price: float,
        *,
        fill_size: float,
        mark_sl: bool = False,
        mark_be: bool = False,
        mark_tp2: bool = False,
        expired: bool = False,
    ) -> SimulatedTrade | None:
        nonlocal remaining_size, final_exec, final_fill_size, state
        nonlocal sl_hit, tp2_hit, breakeven_hit, be_exec
        execution = execute_exit(base_price, fill_size)
        if execution is None:
            return None
        if mark_sl:
            sl_hit = True
        if mark_be:
            breakeven_hit = True
            be_exec = execution
        if mark_tp2:
            tp2_hit = True
        remaining_size -= fill_size
        if remaining_size < -1e-9:
            return None
        remaining_size = 0.0 if abs(remaining_size) < 1e-9 else remaining_size
        final_exec = execution
        final_fill_size = fill_size
        state = "TP2_FINAL" if mark_tp2 else "BREAKEVEN" if mark_be else "STOP_FINAL" if mark_sl else "EXPIRED"
        return build_trade(outcome, ts, expired=expired)

    def execute_tp1(ts: int) -> bool:
        nonlocal tp1_hit, tp1_exec, remaining_size, state
        if tp1_hit or remaining_size <= 0:
            return False
        execution = execute_exit(tp1, half_size)
        if execution is None:
            return False
        tp1_exec = execution
        tp1_hit = True
        remaining_size -= half_size
        if remaining_size < -1e-9:
            return False
        remaining_size = max(0.0, remaining_size)
        state = "TP1_PARTIAL"
        return True

    expiry_ts = int(signal_time + max_hold * 60_000)

    for candle in future_candles:
        parsed = _candle_values(candle)
        if parsed is None:
            continue
        timestamp, _open_price, high, low, close = parsed
        if timestamp < signal_time:
            # Never use a candle that opened before the signal close; doing so
            # would inject unavailable intra-candle history into the simulation.
            continue
        close_time = timestamp + M5_MS
        if close_time <= signal_time:
            continue

        if close_time > expiry_ts:
            if previous_close is None or previous_close_time is None:
                return None
            return close_final(
                "EXPIRED",
                previous_close_time,
                previous_close,
                fill_size=remaining_size,
                expired=True,
            )

        if side == "LONG":
            original_sl_touched = low <= stop
            tp1_touched = (not tp1_hit) and high >= tp1
            tp2_touched = high >= tp2
            be_touched = tp1_hit and low <= entry_exec
        else:
            original_sl_touched = high >= stop
            tp1_touched = (not tp1_hit) and low <= tp1
            tp2_touched = low <= tp2
            be_touched = tp1_hit and high >= entry_exec

        if not tp1_hit:
            if original_sl_touched and (tp1_touched or tp2_touched):
                # Same-bar competition before TP1: configurable ordering.
                if rule == "SL_FIRST":
                    return close_final(
                        "SL",
                        close_time,
                        stop,
                        fill_size=remaining_size,
                        mark_sl=True,
                    )
                if not execute_tp1(close_time):
                    return None
                if tp2_touched:
                    return close_final(
                        "TP2",
                        close_time,
                        tp2,
                        fill_size=remaining_size,
                        mark_tp2=True,
                    )
                if side == "LONG":
                    same_bar_be = low <= entry_exec
                else:
                    same_bar_be = high >= entry_exec
                if same_bar_be:
                    # TP1 must execute before the newly armed BE stop can fire.
                    # A conservative interpretation of this state closes the
                    # residual at BE rather than inventing a price path.
                    return close_final(
                        "BE",
                        close_time,
                        entry_exec,
                        fill_size=remaining_size,
                        mark_be=True,
                    )
                previous_close, previous_close_time = close, close_time
                continue

            if tp2_touched:
                # Price reaching TP2 necessarily crosses TP1 in the target
                # direction. With no active protective stop collision, execute
                # the two profit stages in order on the same bar.
                if not execute_tp1(close_time):
                    return None
                return close_final(
                    "TP2",
                    close_time,
                    tp2,
                    fill_size=remaining_size,
                    mark_tp2=True,
                )

            if tp1_touched:
                if not execute_tp1(close_time):
                    return None
                # TP1 is filled first; the residual BE stop is now active.
                be_touched_after_tp1 = low <= entry_exec if side == "LONG" else high >= entry_exec
                if be_touched_after_tp1:
                    return close_final(
                        "BE",
                        close_time,
                        entry_exec,
                        fill_size=remaining_size,
                        mark_be=True,
                    )
                previous_close, previous_close_time = close, close_time
                continue

            if original_sl_touched:
                return close_final(
                    "SL",
                    close_time,
                    stop,
                    fill_size=remaining_size,
                    mark_sl=True,
                )

            previous_close, previous_close_time = close, close_time
            continue

        # TP1_PARTIAL: exactly the residual half remains active. Original SL
        # can never re-open after TP1; only TP2 or breakeven can close it.
        if tp2_touched and be_touched:
            if rule == "SL_FIRST":
                return close_final(
                    "BE",
                    close_time,
                    entry_exec,
                    fill_size=remaining_size,
                    mark_be=True,
                )
            return close_final(
                "TP2",
                close_time,
                tp2,
                fill_size=remaining_size,
                mark_tp2=True,
            )

        if tp2_touched:
            return close_final(
                "TP2",
                close_time,
                tp2,
                fill_size=remaining_size,
                mark_tp2=True,
            )

        if be_touched:
            return close_final(
                "BE",
                close_time,
                entry_exec,
                fill_size=remaining_size,
                mark_be=True,
            )

        previous_close, previous_close_time = close, close_time

    # No trigger before supplied history ends. Close the remaining position at
    # the last valid completed candle rather than leaving an orphan state.
    if previous_close is None or previous_close_time is None:
        return None
    if remaining_size <= 0:
        return None
    return close_final(
        "EXPIRED",
        previous_close_time,
        previous_close,
        fill_size=remaining_size,
        expired=True,
    )
