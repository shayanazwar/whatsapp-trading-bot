from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Iterable, Mapping


ONE_HOUR_MS = 3_600_000
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
    entry_filled_time_ms: int | None = None
    entry_mode: str = "MARKET"
    mae_r: float | None = None
    mfe_r: float | None = None
    mfe_1r_hit: bool = False
    mfe_1_5r_hit: bool = False
    time_to_1r_minutes: float | None = None
    time_to_1_5r_minutes: float | None = None

    @property
    def tp(self) -> float:
        return self.tp1


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _candle_values(candle: Any) -> tuple[int, float, float, float, float] | None:
    try:
        if isinstance(candle, Mapping):
            timestamp = int(float(candle.get("time", candle.get("timestamp", candle.get("openTime")))))
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
    if timestamp <= 0 or not all(isfinite(v) for v in (open_price, high, low, close)):
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
            if (size_d / step_d) != (size_d / step_d).to_integral_value():
                return None
        except (InvalidOperation, ValueError, OverflowError):
            return None
    return size, contract_size


def _quality_snapshot(signal: Mapping[str, Any]) -> dict[str, float]:
    snapshot: dict[str, float] = {}
    for key in (
        "score",
        "confirmation_family_count",
        "confirmation_families_passed",
        "confirmation_families_available",
        "bos_4h_strength",
        "trigger_quality_1h",
        "rvol_1h",
        "rsi",
        "adx_4h",
        "atr_percentile",
        "sl_atr",
        "stop_distance_pct",
        "tp_distance_atr",
        "tp_distance_pct",
        "ema_extension_atr",
        "one_hour_long_votes",
        "one_hour_short_votes",
        "macd_hist_delta",
    ):
        value = signal.get(key)
        if value is None:
            continue
        try:
            snapshot[key] = float(value)
        except (TypeError, ValueError):
            continue
    retest = signal.get("retest") or {}
    if isinstance(retest, Mapping) and retest.get("quality") is not None:
        try:
            snapshot["retest_quality"] = float(retest["quality"])
        except (TypeError, ValueError):
            pass
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
    """Simulate a deterministic single-TP / single-SL trade.

    There is no TP1 partial close, no breakeven transition, and no second
    target. A resolved winner is simply TP before SL.
    """
    try:
        symbol = str(signal.get("symbol") or "UNKNOWN")
        side = str(signal.get("setup") or signal.get("side") or "").upper()
        entry = _number(signal.get("entry"))
        stop = _number(signal.get("stop_loss"))
        tp = _number(signal.get("tp"))
        if tp is None:
            legacy_tp1 = _number(signal.get("tp1"))
            legacy_tp2 = _number(signal.get("tp2"))
            if legacy_tp1 is None or legacy_tp2 is None:
                return None
            if abs(legacy_tp1 - legacy_tp2) > max(1e-12, abs(legacy_tp2) * 1e-9):
                return None
            tp = legacy_tp2
        signal_time = int(signal_close_time_ms)
    except (TypeError, ValueError, OverflowError, AttributeError):
        return None

    if side not in {"LONG", "SHORT"} or entry is None or stop is None or tp is None:
        return None
    planned_risk = abs(entry - stop)
    if planned_risk <= 0 or not isfinite(planned_risk):
        return None
    if side == "LONG" and not (stop < entry < tp):
        return None
    if side == "SHORT" and not (tp < entry < stop):
        return None

    size_data = _position_size(signal)
    if size_data is None:
        return None
    initial_size, contract_size = size_data

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

    rule = _same_bar_rule(same_bar_rule if same_bar_rule is not None else signal.get("same_bar_rule"))
    entry_mode = str(signal.get("entry_mode") or "MARKET").upper()
    limit_price = _number(signal.get("limit_price", signal.get("entry")))
    limit_expiry_minutes = max(1.0, float(signal.get("limit_entry_expiry_minutes", 120.0)))

    entry_exec: float | None = None
    fill_ts: int | None = None
    risk_exec = 0.0
    initial_risk_cash = 0.0
    entry_fee = 0.0
    realized_pnl = 0.0
    gross_pnl = 0.0
    exit_fees = 0.0
    slippage_cash = 0.0
    tp_hit = sl_hit = False
    final_exec: float | None = None
    final_ts: int | None = None
    previous_close: float | None = None
    previous_close_time: int | None = None
    mae_r = 0.0
    mfe_r = 0.0
    mfe_1r_hit = False
    mfe_1_5r_hit = False
    time_to_1r_minutes: float | None = None
    time_to_1_5r_minutes: float | None = None

    def execute_exit(base_price: float) -> float | None:
        nonlocal realized_pnl, gross_pnl, exit_fees, slippage_cash
        if not isfinite(base_price) or base_price <= 0 or entry_exec is None:
            return None
        exit_exec = _adverse_slippage(base_price, side, is_entry=False, slippage_bps=slippage_bps)
        pnl_per_unit = exit_exec - entry_exec if side == "LONG" else entry_exec - exit_exec
        gross = pnl_per_unit * initial_size * contract_size
        fee = abs(exit_exec) * initial_size * contract_size * fee_rate
        slip_cash = abs(exit_exec - base_price) * initial_size * contract_size
        if not all(isfinite(v) for v in (exit_exec, gross, fee, slip_cash)):
            return None
        realized_pnl += gross - fee
        gross_pnl += gross
        exit_fees += fee
        slippage_cash += slip_cash
        return exit_exec

    def build_trade(outcome: str, ts: int | None, expired: bool = False, close_position: bool = True) -> SimulatedTrade | None:
        hold = None if ts is None or fill_ts is None else _hold_minutes(fill_ts, ts)
        net_r = realized_pnl / initial_risk_cash if close_position and initial_risk_cash > 0 else None
        if net_r is not None and not isfinite(net_r):
            net_r = None
        denominator = initial_risk_cash if initial_risk_cash > 0 else 0.0
        fees_r = (entry_fee + exit_fees) / denominator if denominator else 0.0
        slip_r = slippage_cash / denominator if denominator else 0.0
        planned_rr = abs(tp - entry) / planned_risk if planned_risk > 0 else 0.0
        return SimulatedTrade(
            symbol=symbol, side=side, signal_time_ms=signal_time, entry=entry,
            stop_loss=stop, tp1=tp, tp2=tp, planned_rr=planned_rr,
            tp1_hit=tp_hit, tp2_hit=tp_hit, sl_hit=sl_hit, outcome=outcome,
            r_multiple=net_r, exit_time_ms=ts, hold_minutes=hold, fees_r=fees_r,
            slippage_r=slip_r, entry_execution=entry_exec, exit_execution=final_exec,
            expired=expired, regime=str(signal.get("regime") or signal.get("trend_4h") or "UNKNOWN"),
            quality=_quality_snapshot(signal), initial_position_size=initial_size,
            tp1_close_size=0.0, final_close_size=initial_size if close_position else 0.0,
            remaining_position_size=0.0 if close_position else initial_size,
            breakeven_hit=False, original_stop_loss=stop, breakeven_stop=None,
            tp1_execution=final_exec if tp_hit else None, breakeven_execution=None,
            final_exit_execution=final_exec if close_position else None, realized_pnl=realized_pnl,
            gross_pnl=gross_pnl, entry_fee=entry_fee, exit_fees=exit_fees,
            position_contract_size=contract_size, same_bar_rule=rule,
            state="FINALIZED" if close_position else "OPEN", entry_filled_time_ms=fill_ts, entry_mode=entry_mode,
            mae_r=mae_r if fill_ts is not None else None, mfe_r=mfe_r if fill_ts is not None else None,
            mfe_1r_hit=mfe_1r_hit, mfe_1_5r_hit=mfe_1_5r_hit,
            time_to_1r_minutes=time_to_1r_minutes, time_to_1_5r_minutes=time_to_1_5r_minutes,
        )

    expiry_ts = int(signal_time + max_hold * 60_000)
    limit_expiry_ts = int(signal_time + limit_expiry_minutes * 60_000)

    for candle in future_candles:
        parsed = _candle_values(candle)
        if parsed is None:
            continue
        timestamp, _open_price, high, low, close = parsed
        close_time = timestamp + ONE_HOUR_MS
        if close_time <= signal_time:
            continue

        # Passive retest limit: no position exists until price actually trades through
        # the requested limit. If not filled within the expiry window, the order dies.
        if entry_exec is None:
            if entry_mode == "LIMIT":
                if timestamp > limit_expiry_ts:
                    return None
                if limit_price is None or not isfinite(limit_price) or limit_price <= 0:
                    return None
                fills = low <= limit_price if side == "LONG" else high >= limit_price
                if not fills:
                    continue
                entry = float(limit_price)
                entry_exec = entry  # passive fill: no adverse market-order slippage
                fill_ts = timestamp
            else:
                entry_exec = _adverse_slippage(entry, side, is_entry=True, slippage_bps=slippage_bps)
                fill_ts = signal_time

            if not isfinite(entry_exec) or entry_exec <= 0:
                return None
            if side == "LONG" and not (stop < entry_exec < tp):
                return None
            if side == "SHORT" and not (tp < entry_exec < stop):
                return None
            risk_exec = abs(entry_exec - stop)
            initial_risk_cash = risk_exec * initial_size * contract_size
            if risk_exec <= 0 or initial_risk_cash <= 0 or not isfinite(initial_risk_cash):
                return None
            entry_fee = abs(entry_exec) * initial_size * contract_size * fee_rate
            realized_pnl = -entry_fee
            slippage_cash = abs(entry_exec - entry) * initial_size * contract_size
            # A limit order can fill inside this candle; without intrabar sequencing,
            # the same deterministic SL_FIRST/TP_FIRST rule is used after the fill.

        if close_time > expiry_ts:
            if previous_close is None or previous_close_time is None:
                previous_close = close
                previous_close_time = close_time
            final_exec = execute_exit(previous_close)
            if final_exec is None:
                return None
            return build_trade("EXPIRED", previous_close_time, expired=True)

        # Track normalized adverse/favorable excursion before resolving the bar.
        if risk_exec > 0 and entry_exec is not None:
            if side == "LONG":
                mae_r = max(mae_r, max(0.0, entry_exec - low) / risk_exec)
                current_mfe = max(0.0, high - entry_exec) / risk_exec
            else:
                mae_r = max(mae_r, max(0.0, high - entry_exec) / risk_exec)
                current_mfe = max(0.0, entry_exec - low) / risk_exec
            mfe_r = max(mfe_r, current_mfe)
            if current_mfe >= 1.0 and not mfe_1r_hit:
                mfe_1r_hit = True
                time_to_1r_minutes = _hold_minutes(fill_ts, close_time)
            if current_mfe >= 1.5 and not mfe_1_5r_hit:
                mfe_1_5r_hit = True
                time_to_1_5r_minutes = _hold_minutes(fill_ts, close_time)

        if side == "LONG":
            sl_touched = low <= stop
            tp_touched = high >= tp
        else:
            sl_touched = high >= stop
            tp_touched = low <= tp

        if sl_touched and tp_touched:
            first = "SL" if rule == "SL_FIRST" else "TP"
        elif tp_touched:
            first = "TP"
        elif sl_touched:
            first = "SL"
        else:
            first = None

        if first == "TP":
            final_exec = execute_exit(tp)
            if final_exec is None:
                return None
            tp_hit = True
            final_ts = close_time
            return build_trade("TP", final_ts)
        if first == "SL":
            final_exec = execute_exit(stop)
            if final_exec is None:
                return None
            sl_hit = True
            final_ts = close_time
            return build_trade("SL", final_ts)

        previous_close = close
        previous_close_time = close_time

    if entry_exec is None:
        return None
    if previous_close is None or previous_close_time is None:
        return None
    return build_trade("OPEN", previous_close_time, expired=False, close_position=False)

