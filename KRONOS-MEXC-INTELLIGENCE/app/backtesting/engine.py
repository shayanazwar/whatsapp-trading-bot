from __future__ import annotations

from datetime import timedelta
from typing import Callable

import numpy as np
import pandas as pd

from app.market.candles import aggregate_12h_from_4h, aggregate_4h_from_1h, validate_candles
from app.simulation.costs import net_reward_risk
from app.strategy.engine import analyze_multitimeframe


def _metrics(trades: list[dict], equity_curve: list[dict], start_equity: float,
             unresolved_positions: list[dict] | None = None) -> dict:
    unresolved_positions = unresolved_positions or []
    pnls = np.array([float(t["net_pnl_usdt"]) for t in trades], dtype=float)
    rs = np.array([float(t["net_r"]) for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    balances = np.array([float(x["equity"]) for x in equity_curve], dtype=float)
    peak = np.maximum.accumulate(np.r_[start_equity, balances])
    drawdowns = (peak[1:] - balances) / np.maximum(peak[1:], 1e-12) if len(balances) else np.array([])
    consecutive = 0
    max_consecutive_losses = 0
    for pnl in pnls:
        consecutive = consecutive + 1 if pnl < 0 else 0
        max_consecutive_losses = max(max_consecutive_losses, consecutive)
    holding_hours = [float(t["holding_hours"]) for t in trades if t.get("holding_hours") is not None]
    monthly: dict[str, float] = {}
    for trade in trades:
        month = str(trade.get("exit_time", ""))[:7]
        if month:
            monthly[month] = monthly.get(month, 0.0) + float(trade["net_pnl_usdt"])
    missing_funding = any(t.get("funding_status") != "HISTORICAL_EVENTS_APPLIED" for t in trades)
    return {
        "trades": len(trades),
        "wins": int((pnls > 0).sum()),
        "losses": int((pnls < 0).sum()),
        "breakeven": int((pnls == 0).sum()),
        "win_rate": float((pnls > 0).mean()) if len(pnls) else None,
        "net_profit_usdt": float(pnls.sum()) if len(pnls) else 0.0,
        "net_return_pct": float((balances[-1] / start_equity - 1) * 100) if len(balances) else 0.0,
        "average_r": float(rs.mean()) if len(rs) else None,
        "expectancy_r": float(rs.mean()) if len(rs) else None,
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and abs(losses.sum()) else None,
        "max_drawdown_pct": float(drawdowns.max() * 100) if len(drawdowns) else 0.0,
        "gross_profit_usdt": float(wins.sum()) if len(wins) else 0.0,
        "gross_loss_usdt": float(abs(losses.sum())) if len(losses) else 0.0,
        "fees_usdt": float(sum(float(t.get("fees_usdt") or 0) for t in trades)),
        "slippage_usdt": float(sum(float(t.get("slippage_usdt") or 0) for t in trades)),
        "funding_usdt": (None if missing_funding else float(sum(float(t.get("funding_usdt") or 0) for t in trades))),
        "funding_data_complete_for_closed_trades": not missing_funding,
        "long_trades": sum(1 for t in trades if t["side"] == "LONG"),
        "short_trades": sum(1 for t in trades if t["side"] == "SHORT"),
        "tp1_touched": sum(1 for t in trades if t.get("tp1_touched")),
        "tp2_exits": sum(1 for t in trades if t.get("exit_type") == "TP2"),
        "stop_exits": sum(1 for t in trades if t.get("exit_type") in {"STOP", "STOP_AFTER_TP1", "AMBIGUOUS_STOP_FIRST"}),
        "average_holding_hours": float(np.mean(holding_hours)) if holding_hours else None,
        "max_consecutive_losses": max_consecutive_losses,
        "monthly_net_profit_usdt": monthly,
        "unresolved_positions": len(unresolved_positions),
        "unrealized_mark_to_market_usdt": float(sum(float(x.get("estimated_net_pnl_usdt") or 0) for x in unresolved_positions)),
    }


def run_event_backtest(*, symbol: str, candles_1h: pd.DataFrame, candles_4h: pd.DataFrame,
                       candles_1d: pd.DataFrame, contract: dict, start_time, end_time,
                       starting_equity: float = 1000, risk_pct: float = 0.5,
                       slippage_bps: float = 3, min_net_rr: float = 1.5,
                       use_kronos_filter: bool = False,
                       kronos_filter: Callable[[pd.DataFrame, str], dict] | None = None,
                       funding_events: list[dict] | None = None) -> dict:
    """Replay decisions using only completed candles and enter no earlier than next bar open."""
    h1 = validate_candles(candles_1h)
    h4 = validate_candles(candles_4h)
    d1 = validate_candles(candles_1d)
    h12 = aggregate_12h_from_4h(h4)
    if len(h12) < 55 or len(d1) < 55:
        raise ValueError("Backtest requires at least 55 completed 12H and 1D candles after warm-up")
    fee = contract.get("takerFeeRate")
    fee_mode = contract.get("feeRateMode") or "NORMAL"
    fee_type = contract.get("feeRateType") or "BASE"
    if fee_mode != "NORMAL" or fee_type != "BASE":
        raise ValueError(
            f"Unsupported contract fee schedule for reproducible backtest: mode={fee_mode}, type={fee_type}"
        )
    contract_size = float(contract.get("contractSize") or 0)
    vol_unit = float(contract.get("volUnit") or 1)
    if fee is None or contract_size <= 0:
        raise ValueError("Verified MEXC contract takerFeeRate and contractSize are required")
    fee = float(fee)
    equity = float(starting_equity)
    trades: list[dict] = []
    unresolved_positions: list[dict] = []
    equity_curve: list[dict] = []
    warnings: list[str] = []
    i = 0
    start_ts = pd.Timestamp(start_time)
    end_ts = pd.Timestamp(end_time)
    while i < len(h1) - 1:
        row = h1.iloc[i]
        decision_time = row["timestamp"] + pd.Timedelta(hours=1)
        if decision_time < start_ts:
            i += 1
            continue
        if decision_time > end_ts:
            break
        # Only include HTF bars whose close time is no later than this decision timestamp.
        d1_visible = d1.loc[d1["timestamp"] + pd.Timedelta(days=1) <= decision_time]
        h4_visible = h4.loc[h4["timestamp"] + pd.Timedelta(hours=4) <= decision_time]
        h12_visible = h12.loc[h12["timestamp"] + pd.Timedelta(hours=12) <= decision_time]
        h1_visible = h1.iloc[:i + 1]
        try:
            decision = analyze_multitimeframe(symbol, d1_visible, h12_visible, h4_visible, h1_visible)
        except (ValueError, IndexError):
            i += 1
            continue
        if decision.decision not in {"LONG_CANDIDATE", "SHORT_CANDIDATE"}:
            i += 1
            continue
        side = decision.side
        if use_kronos_filter:
            if kronos_filter is None:
                warnings.append("Kronos-filtered backtest requested but no inference adapter was supplied")
                raise ValueError(warnings[-1])
            forecast_decision = kronos_filter(h1_visible, side)
            if not forecast_decision.get("approved"):
                i += 1
                continue
        # Realistic bar-close signal, next candle open execution assumption.
        next_bar = h1.iloc[i + 1]
        entry = float(next_bar["open"])
        stop = float(decision.stop)
        tp1 = float(decision.tp1)
        tp2 = float(decision.tp2)
        geometry_ok = stop < entry < tp2 if side == "LONG" else tp2 < entry < stop
        if not geometry_ok:
            i += 1
            continue
        net_geometry = net_reward_risk(
            entry=entry, stop=stop, target=tp2, side=side,
            contract_size=contract_size, contracts=1,
            taker_fee_rate=fee, slippage_bps=slippage_bps,
        )
        if net_geometry["net_rr"] < min_net_rr:
            i += 1
            continue
        risk_usdt = equity * risk_pct / 100
        risk_per_contract = abs(entry - stop) * contract_size
        round_trip_per_contract = (entry + stop) * contract_size * (fee + slippage_bps / 10_000)
        contracts = np.floor(risk_usdt / (risk_per_contract + round_trip_per_contract) / vol_unit) * vol_unit
        if contracts <= 0:
            i += 1
            continue
        qty = contracts * contract_size
        entry_fee = entry * qty * fee
        entry_slippage = entry * qty * slippage_bps / 10_000
        tp1_seen = False
        tp1_index: int | None = None
        exit_type = "OPEN_AT_END"
        exit_price = float(h1.iloc[-1]["close"])
        exit_index = len(h1) - 1
        for j in range(i + 1, len(h1)):
            bar = h1.iloc[j]
            hi, lo = float(bar["high"]), float(bar["low"])
            if side == "LONG":
                stop_hit, tp1_hit, tp2_hit = lo <= stop, hi >= tp1, hi >= tp2
            else:
                stop_hit, tp1_hit, tp2_hit = hi >= stop, lo <= tp1, lo <= tp2
            if stop_hit and (tp1_hit or tp2_hit):
                # If TP1 was reached on an earlier completed bar, its 50% exit is
                # already realized; only the remaining half is resolved stop-first.
                exit_type = "STOP_AFTER_TP1" if tp1_seen else "AMBIGUOUS_STOP_FIRST"
                exit_price, exit_index = stop, j
                break
            if stop_hit:
                exit_type, exit_price, exit_index = ("STOP_AFTER_TP1" if tp1_seen else "STOP"), stop, j
                break
            if tp2_hit:
                tp1_seen = True
                tp1_index = j
                exit_type, exit_price, exit_index = "TP2", tp2, j
                break
            if tp1_hit and not tp1_seen:
                tp1_seen = True
                tp1_index = j
        if exit_type == "OPEN_AT_END":
            warnings.append(f"{symbol}: one position remained open at the end; mark-to-market is reported separately")
        if exit_type == "TP2":
            exit_notional_price = 0.5 * tp1 + 0.5 * tp2
        elif exit_type == "STOP_AFTER_TP1":
            exit_notional_price = 0.5 * tp1 + 0.5 * stop
        elif exit_type == "OPEN_AT_END" and tp1_seen:
            exit_notional_price = 0.5 * tp1 + 0.5 * exit_price
        else:
            exit_notional_price = exit_price
        exit_fee = exit_notional_price * qty * fee
        exit_slippage = exit_notional_price * qty * slippage_bps / 10_000
        if exit_type == "TP2":
            gross_pnl = contracts * contract_size * (0.5 * (tp1 - entry if side == "LONG" else entry - tp1)
                                                       + 0.5 * (tp2 - entry if side == "LONG" else entry - tp2))
        elif exit_type == "STOP_AFTER_TP1":
            gross_pnl = contracts * contract_size * 0.5 * ((tp1 - entry) if side == "LONG" else (entry - tp1))
        elif exit_type == "OPEN_AT_END" and tp1_seen:
            remaining = exit_price - entry if side == "LONG" else entry - exit_price
            gross_pnl = contracts * contract_size * (0.5 * abs(tp1 - entry) + 0.5 * remaining)
        else:
            gross_pnl = contracts * contract_size * ((exit_price - entry) if side == "LONG" else (entry - exit_price))
        fees = entry_fee + exit_fee
        slippage = entry_slippage + exit_slippage
        funding_cost = 0.0
        funding_known = bool(funding_events)
        if funding_events:
            t0 = next_bar["timestamp"]
            t1 = h1.iloc[exit_index]["timestamp"]
            for event in funding_events:
                settle = pd.to_datetime(event.get("settleTime"), unit="ms", utc=True)
                if t0 <= settle <= t1:
                    rate = float(event["fundingRate"])
                    # Positive rate: longs pay, shorts receive. After TP1 only half remains open.
                    exposure_qty = qty * (0.5 if tp1_index is not None and settle >= h1.iloc[tp1_index]["timestamp"] else 1.0)
                    direction_sign = 1 if side == "LONG" else -1
                    funding_cost += exposure_qty * entry * rate * direction_sign
        net_pnl = gross_pnl - fees - slippage - funding_cost
        net_r = net_pnl / max(risk_usdt, 1e-12)
        holding_hours = max(0.0, (pd.Timestamp(h1.iloc[exit_index]["timestamp"]) - pd.Timestamp(next_bar["timestamp"])).total_seconds() / 3600)
        trade = {
            "entry_time": next_bar["timestamp"].isoformat(),
            "exit_time": h1.iloc[exit_index]["timestamp"].isoformat(),
            "holding_hours": holding_hours,
            "tp1_touched": bool(tp1_seen),
            "side": side, "setup": decision.setup_4h,
            "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "contracts": float(contracts), "base_quantity": float(qty),
            "notional_usdt": float(qty * entry), "exit_price": float(exit_price),
            "exit_type": exit_type, "gross_pnl_usdt": float(gross_pnl),
            "fees_usdt": float(fees), "slippage_usdt": float(slippage),
            "funding_usdt": float(funding_cost) if funding_known else None,
            "funding_status": "HISTORICAL_EVENTS_APPLIED" if funding_known else "NOT_AVAILABLE_NOT_ASSUMED_ZERO",
            "net_pnl_usdt": float(net_pnl), "net_r": float(net_r),
            "strategy_score": float(decision.score),
            "kronos_filter_used": bool(use_kronos_filter),
        }
        if exit_type == "OPEN_AT_END":
            unresolved_positions.append({
                **trade,
                "status": "UNRESOLVED_MARK_TO_MARKET_NOT_REALIZED",
                "estimated_net_pnl_usdt": float(net_pnl),
            })
            break
        trades.append(trade)
        equity += net_pnl
        equity_curve.append({"time": h1.iloc[exit_index]["timestamp"].isoformat(), "equity": float(equity)})
        i = max(i + 1, exit_index + 1)
    metrics = _metrics(trades, equity_curve, starting_equity, unresolved_positions)
    metrics["funding_data_status"] = "HISTORICAL_EVENTS_APPLIED" if funding_events else "UNAVAILABLE_OR_NOT_FETCHED"
    metrics["kronos_filter_used"] = bool(use_kronos_filter)
    metrics["survivorship_bias_warning"] = "Current contract universe is not point-in-time historical membership."
    return {
        "symbol": symbol,
        "start_time": start_ts.isoformat(),
        "end_time": end_ts.isoformat(),
        "metrics": metrics,
        "trades": trades,
        "unresolved_positions": unresolved_positions,
        "equity_curve": equity_curve,
        "warnings": warnings,
    }
