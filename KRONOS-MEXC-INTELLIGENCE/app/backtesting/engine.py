from __future__ import annotations

from typing import Callable
import hashlib
import json

import numpy as np
import pandas as pd

from app.market.candles import (
    aggregate_12h_from_4h,
    validate_candle_continuity,
)
from app.risk.manager import quantize_price, resolve_contract_risk_limits, size_position
from app.simulation.costs import net_reward_risk
from app.strategy.engine import analyze_multitimeframe


def _funding_cost_until(*, events: list[dict] | None, start_time, end_time,
                        h1: pd.DataFrame, side: str, base_quantity: float,
                        tp1_time=None, tp1_exit_fraction: float = 0.5) -> tuple[float | None, str]:
    """Apply historical rates using event fairPrice when present; otherwise use a
    clearly-labelled last-completed-1H-close proxy. Missing history is never zero-filled."""
    if events is None:
        return None, "UNAVAILABLE_OR_FETCH_FAILED"
    if not np.isfinite(tp1_exit_fraction) or not 0.0 < tp1_exit_fraction < 1.0:
        return None, "INVALID_TP1_EXIT_FRACTION"
    start = pd.Timestamp(start_time)
    end = pd.Timestamp(end_time)
    total = 0.0
    used_proxy = False
    for event in events:
        try:
            settle = pd.to_datetime(int(event["settleTime"]), unit="ms", utc=True)
            if not (start <= settle <= end):
                continue
            rate = float(event["fundingRate"])
            if not np.isfinite(rate):
                return None, "INCOMPLETE_INVALID_FUNDING_RATE"
            fair = event.get("fairPrice")
            if fair is not None:
                fair_price = float(fair)
            else:
                completed = h1.loc[h1["timestamp"] + pd.Timedelta(hours=1) <= settle]
                if completed.empty:
                    return None, "INCOMPLETE_NO_SETTLEMENT_PRICE_PROXY"
                fair_price = float(completed.iloc[-1]["close"])
                used_proxy = True
            if not np.isfinite(fair_price) or fair_price <= 0:
                return None, "INCOMPLETE_INVALID_SETTLEMENT_PRICE"
            # If TP1 was confirmed on an earlier completed candle, only half
            # the base quantity remained exposed at this funding timestamp.
            # TP1's intrabar sequence is unknowable from a 1H OHLC bar. At an
            # exact settlement/TP1-close timestamp, conservatively charge full
            # quantity; half exposure applies only to later settlements.
            exposure = base_quantity * (
                1.0 - tp1_exit_fraction
                if tp1_time is not None and settle > pd.Timestamp(tp1_time)
                else 1.0
            )
            direction = 1.0 if side == "LONG" else -1.0  # positive funding: long pays, short receives
            total += exposure * fair_price * rate * direction
        except (KeyError, TypeError, ValueError, OverflowError):
            return None, "INCOMPLETE_MALFORMED_FUNDING_EVENT"
    if used_proxy:
        return total, "HISTORICAL_RATE_WITH_HOURLY_CLOSE_PROXY"
    if any(pd.Timestamp(start_time) <= pd.to_datetime(int(e["settleTime"]), unit="ms", utc=True) <= pd.Timestamp(end_time)
           for e in events if e.get("settleTime") is not None):
        return total, "HISTORICAL_RATE_WITH_EVENT_FAIR_PRICE"
    return 0.0, "NO_SETTLEMENT_WITHIN_TRADE"


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
    exact_funding_status = {
        "HISTORICAL_RATE_WITH_EVENT_FAIR_PRICE",
        "NO_SETTLEMENT_WITHIN_TRADE",
    }
    proxy_funding_status = "HISTORICAL_RATE_WITH_HOURLY_CLOSE_PROXY"
    missing_funding = any(t.get("funding_status") not in exact_funding_status | {proxy_funding_status}
                          for t in trades)
    proxy_funding = any(t.get("funding_status") == proxy_funding_status for t in trades)
    realized_profit = float(pnls.sum()) if len(pnls) else 0.0
    ending_mtm_equity = float(balances[-1]) if len(balances) else start_equity
    return {
        "trades": len(trades),
        "wins": int((pnls > 0).sum()),
        "losses": int((pnls < 0).sum()),
        "breakeven": int((pnls == 0).sum()),
        "win_rate": float((pnls > 0).mean()) if len(pnls) else None,
        "net_profit_usdt": realized_profit,
        "net_return_pct": float(realized_profit / start_equity * 100) if start_equity else None,
        "ending_mark_to_market_equity_usdt": ending_mtm_equity,
        "average_r": float(rs.mean()) if len(rs) else None,
        "expectancy_r": float(rs.mean()) if len(rs) else None,
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and abs(losses.sum()) else None,
        "max_drawdown_pct": float(drawdowns.max() * 100) if len(drawdowns) else 0.0,
        "drawdown_method": "BAR_CLOSE_MARK_TO_MARKET_PLUS_REALIZED_EQUITY",
        "gross_profit_usdt": float(wins.sum()) if len(wins) else 0.0,
        "gross_loss_usdt": float(abs(losses.sum())) if len(losses) else 0.0,
        "fees_usdt": float(sum(float(t.get("fees_usdt") or 0) for t in trades)),
        "slippage_usdt": float(sum(float(t.get("slippage_usdt") or 0) for t in trades)),
        "funding_usdt": (None if missing_funding else float(sum(float(t.get("funding_usdt") or 0) for t in trades))),
        "funding_data_complete_for_closed_trades": not missing_funding and not proxy_funding,
        "funding_is_estimated_for_any_trade": proxy_funding,
        "funding_data_quality": (
            "INCOMPLETE_OR_UNAVAILABLE" if missing_funding else
            "ESTIMATED_USING_LAST_COMPLETED_1H_CLOSE" if proxy_funding else "EVENT_FAIR_PRICE_OR_NO_SETTLEMENT"
        ),
        "funding_valuation_methods": sorted({str(t.get("funding_status")) for t in trades}),
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


def _mark_to_market_pnl(*, side: str, entry: float, mark: float, stop: float, tp1: float,
                        tp1_seen: bool, contracts: float, contract_size: float,
                        entry_fee: float, entry_slippage: float, fee_rate: float,
                        slippage_bps: float, funding_cost: float,
                        tp1_exit_fraction: float = 0.5) -> float:
    direction = 1.0 if side == "LONG" else -1.0
    base_qty = contracts * contract_size
    if tp1_seen:
        f1, f2 = tp1_exit_fraction, 1.0 - tp1_exit_fraction
        realized = base_qty * f1 * direction * (tp1 - entry)
        unrealized = base_qty * f2 * direction * (mark - entry)
        exit_notional = base_qty * (f1 * tp1 + f2 * mark)
    else:
        realized = 0.0
        unrealized = base_qty * direction * (mark - entry)
        exit_notional = base_qty * mark
    estimated_exit_cost = exit_notional * (fee_rate + slippage_bps / 10_000)
    return realized + unrealized - entry_fee - entry_slippage - estimated_exit_cost - funding_cost


def run_event_backtest(*, symbol: str, candles_1h: pd.DataFrame, candles_4h: pd.DataFrame,
                       candles_1d: pd.DataFrame, contract: dict, start_time, end_time,
                       starting_equity: float = 1000, risk_pct: float = 0.5,
                       slippage_bps: float = 3, min_net_rr: float = 1.5,
                       use_kronos_filter: bool = False,
                       kronos_filter: Callable[[pd.DataFrame, str, dict], dict] | None = None,
                       funding_events: list[dict] | None = None,
                       leverage: int = 2, margin_buffer_pct: float = 10.0,
                       tp2_r_multiple: float = 2.5, tp1_exit_fraction: float = 0.5) -> dict:
    """Chronological backtest using complete continuous candles and next-bar-open entries.

    A 1H OHLC bar cannot reveal intrabar order; any simultaneous stop/target touch
    is conservatively resolved at the stop. Stop gaps fill at the adverse bar open.
    """
    if not np.isfinite(tp2_r_multiple) or not 1.0 < tp2_r_multiple <= 10.0:
        raise ValueError("tp2_r_multiple must be finite and in (1, 10]")
    if not np.isfinite(tp1_exit_fraction) or not 0.0 < tp1_exit_fraction < 1.0:
        raise ValueError("tp1_exit_fraction must be finite and in (0, 1)")
    if funding_events is None:
        raise ValueError(
            "Historical funding data is unavailable or incomplete. A net-P&L backtest cannot "
            "assume zero funding; retry after the MEXC funding-history fetch succeeds."
        )
    h1 = validate_candle_continuity(candles_1h, 3600)
    h4 = validate_candle_continuity(candles_4h, 14_400)
    d1 = validate_candle_continuity(candles_1d, 86_400)
    h12 = aggregate_12h_from_4h(h4)
    if len(h12) < 200 or len(d1) < 200:
        raise ValueError("Backtest requires at least 200 completed 12H and 1D warm-up candles")
    fee = contract.get("takerFeeRate")
    fee_mode = contract.get("feeRateMode") or "NORMAL"
    fee_type = contract.get("feeRateType") or "BASE"
    if fee_mode != "NORMAL" or fee_type != "BASE":
        raise ValueError(f"Unsupported contract fee schedule: mode={fee_mode}, type={fee_type}")
    contract_size = float(contract.get("contractSize") or 0)
    vol_unit = float(contract.get("volUnit") or 0)
    if fee is None or contract_size <= 0 or vol_unit <= 0:
        raise ValueError("Verified MEXC takerFeeRate, contractSize and volUnit are required")
    fee = float(fee)
    tick = float(contract.get("priceUnit") or 0)
    if (not np.isfinite(fee) or not 0 <= fee < 1 or
            not np.isfinite(contract_size) or contract_size <= 0 or
            not np.isfinite(vol_unit) or vol_unit <= 0 or
            not np.isfinite(tick) or tick <= 0):
        raise ValueError("Contract fee, contractSize, volUnit and priceUnit must be finite and valid")
    min_vol = float(contract.get("minVol") or vol_unit)
    risk_limits = resolve_contract_risk_limits(contract)
    max_contracts = risk_limits["max_contracts"]
    risk_mode = risk_limits["risk_limit_mode"]

    equity = float(starting_equity)
    trades: list[dict] = []
    unresolved_positions: list[dict] = []
    equity_curve: list[dict] = []
    warnings: list[str] = [
        "Current contract metadata is used; historical fee/tick/risk-tier/leverage revisions are not point-in-time reconstructed.",
        "No historical order-book queue priority, actual partial fills or full cross-margin liquidation replay is available.",
    ]
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
        d1_visible = d1.loc[d1["timestamp"] + pd.Timedelta(days=1) <= decision_time]
        h4_visible = h4.loc[h4["timestamp"] + pd.Timedelta(hours=4) <= decision_time]
        h12_visible = h12.loc[h12["timestamp"] + pd.Timedelta(hours=12) <= decision_time]
        h1_visible = h1.iloc[:i + 1]
        try:
            decision = analyze_multitimeframe(
                symbol, d1_visible, h12_visible, h4_visible, h1_visible,
                tp2_r_multiple=tp2_r_multiple,
            )
        except (ValueError, IndexError):
            i += 1
            continue
        if decision.decision not in {"LONG_CANDIDATE", "SHORT_CANDIDATE"}:
            i += 1
            continue
        side = decision.side
        forecast_decision = None
        if use_kronos_filter and kronos_filter is None:
            raise ValueError("Kronos-filtered backtest requested without an inference adapter")
        next_bar = h1.iloc[i + 1]
        entry_raw = float(next_bar["open"])
        entry = quantize_price(entry_raw, tick, "ceil" if side == "LONG" else "floor") if tick > 0 else entry_raw
        stop_raw = float(decision.stop)
        stop = quantize_price(stop_raw, tick, "floor" if side == "LONG" else "ceil") if tick > 0 else stop_raw
        geometry_ok = stop < entry if side == "LONG" else entry < stop
        if not geometry_ok:
            i += 1
            continue
        risk_distance = abs(entry - stop)
        tp1_raw = entry + risk_distance if side == "LONG" else entry - risk_distance
        tp2_raw = entry + tp2_r_multiple * risk_distance if side == "LONG" else entry - tp2_r_multiple * risk_distance
        tp1 = quantize_price(tp1_raw, tick, "ceil" if side == "LONG" else "floor") if tick > 0 else tp1_raw
        tp2 = quantize_price(tp2_raw, tick, "ceil" if side == "LONG" else "floor") if tick > 0 else tp2_raw
        net_geometry = net_reward_risk(
            entry=entry, stop=stop, target=tp2, target_1=tp1,
            target_1_fraction=tp1_exit_fraction, side=side,
            contract_size=contract_size, contracts=1,
            taker_fee_rate=fee, slippage_bps=slippage_bps,
        )
        if net_geometry["net_rr"] < min_net_rr:
            i += 1
            continue
        if use_kronos_filter:
            # The forecast must be evaluated against this historical candidate's
            # actual next-bar entry, stop and targets. Direction-only filtering is
            # insufficient: a bearish path can still hit a short stop first.
            forecast_decision = kronos_filter(
                h1_visible, side,
                {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2},
            )
            if not forecast_decision.get("approved"):
                i += 1
                continue
        maintenance = risk_limits["maintenance_margin_rate"]
        initial_margin = risk_limits["initial_margin_rate"]
        try:
            plan = size_position(
                equity=equity, risk_pct=risk_pct, entry=entry, stop=stop, side=side,
                contract_size=contract_size, vol_unit=vol_unit, leverage=leverage,
                taker_fee_rate=fee, slippage_bps=slippage_bps,
                maintenance_margin_rate=float(maintenance),
                initial_margin_rate=float(initial_margin),
                available_margin_usdt=equity, margin_buffer_pct=margin_buffer_pct,
                min_contracts=min_vol, max_contracts=max_contracts,
                max_leverage=int(risk_limits["max_leverage"]),
            )
        except ValueError as exc:
            warnings.append(f"{symbol}: position skipped by risk/margin/contract limits: {exc}")
            i += 1
            continue
        contracts = float(plan.contracts)
        qty = contracts * contract_size
        risk_budget_usdt = float(plan.risk_budget_usdt)
        risk_usdt = float(plan.estimated_stop_loss_usdt)
        if not np.isfinite(risk_usdt) or risk_usdt <= 0:
            raise ValueError("Position sizing returned invalid modeled stop-loss risk")
        entry_fee = entry * qty * fee
        entry_slippage = entry * qty * slippage_bps / 10_000
        tp1_seen = False
        tp1_index: int | None = None
        exit_type = "OPEN_AT_END"
        exit_price = float(h1.iloc[-1]["close"])
        exit_index = len(h1) - 1
        mark_prices: list[tuple[int, float, bool]] = []
        for j in range(i + 1, len(h1)):
            bar = h1.iloc[j]
            op, hi, lo, cl = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
            if side == "LONG":
                stop_hit, tp1_hit, tp2_hit = lo <= stop, hi >= tp1, hi >= tp2
            else:
                stop_hit, tp1_hit, tp2_hit = hi >= stop, lo <= tp1, lo <= tp2
            any_target = tp1_hit or tp2_hit
            if not stop_hit:
                # Include the adverse intrabar excursion in drawdown rather than
                # observing only candle closes. This is deliberately conservative:
                # OHLC cannot reveal the exact event ordering within the bar.
                adverse_mark = lo if side == "LONG" else hi
                mark_open_time = bar["timestamp"]
                funding_to_open, funding_to_open_status = _funding_cost_until(
                    events=funding_events, start_time=next_bar["timestamp"], end_time=mark_open_time,
                    h1=h1, side=side, base_quantity=qty,
                    tp1_time=(h1.iloc[tp1_index]["timestamp"] + pd.Timedelta(hours=1))
                    if tp1_index is not None else None,
                    tp1_exit_fraction=tp1_exit_fraction,
                )
                if funding_to_open is None:
                    raise ValueError(
                        f"Cannot value funding before {mark_open_time}: {funding_to_open_status}"
                    )
                adverse_mtm = _mark_to_market_pnl(
                    side=side, entry=entry, mark=adverse_mark, stop=stop, tp1=tp1,
                    tp1_seen=tp1_seen, contracts=contracts, contract_size=contract_size,
                    entry_fee=entry_fee, entry_slippage=entry_slippage, fee_rate=fee,
                    slippage_bps=slippage_bps, funding_cost=float(funding_to_open or 0),
                    tp1_exit_fraction=tp1_exit_fraction,
                )
                equity_curve.append({
                    "time": mark_open_time.isoformat(),
                    "equity": float(equity + adverse_mtm),
                    "kind": "INTRABAR_ADVERSE_ESTIMATE",
                })
            if stop_hit and any_target:
                exit_type = "STOP_AFTER_TP1" if tp1_seen else "AMBIGUOUS_STOP_FIRST"
                # A stop-market fill can gap beyond the trigger; never assume the trigger price.
                exit_price = min(stop, op) if side == "LONG" else max(stop, op)
                exit_index = j
                break
            if stop_hit:
                exit_type = "STOP_AFTER_TP1" if tp1_seen else "STOP"
                exit_price = min(stop, op) if side == "LONG" else max(stop, op)
                exit_index = j
                break
            if tp2_hit:
                if not tp1_seen:
                    tp1_seen = True
                    tp1_index = j
                exit_type, exit_price, exit_index = "TP2", tp2, j
                break
            if tp1_hit and not tp1_seen:
                tp1_seen = True
                tp1_index = j
            mark_prices.append((j, cl, tp1_seen))
            mark_time = bar["timestamp"] + pd.Timedelta(hours=1)
            funding_so_far, _ = _funding_cost_until(
                events=funding_events, start_time=next_bar["timestamp"], end_time=mark_time,
                h1=h1, side=side, base_quantity=qty,
                tp1_time=(h1.iloc[tp1_index]["timestamp"] + pd.Timedelta(hours=1)) if tp1_index is not None else None,
                tp1_exit_fraction=tp1_exit_fraction,
            )
            mtm = _mark_to_market_pnl(
                side=side, entry=entry, mark=cl, stop=stop, tp1=tp1, tp1_seen=tp1_seen,
                contracts=contracts, contract_size=contract_size,
                entry_fee=entry_fee, entry_slippage=entry_slippage, fee_rate=fee,
                slippage_bps=slippage_bps, funding_cost=float(funding_so_far or 0),
                tp1_exit_fraction=tp1_exit_fraction,
            )
            equity_curve.append({"time": mark_time.isoformat(), "equity": float(equity + mtm), "kind": "MARK_TO_MARKET"})

        if exit_type == "OPEN_AT_END":
            warnings.append(f"{symbol}: one position remained open at the end; mark-to-market is reported separately")
        if exit_type == "TP2":
            f1, f2 = tp1_exit_fraction, 1.0 - tp1_exit_fraction
            exit_notional_price = f1 * tp1 + f2 * tp2
            gross_pnl = qty * (f1 * (tp1 - entry if side == "LONG" else entry - tp1)
                               + f2 * (tp2 - entry if side == "LONG" else entry - tp2))
        elif exit_type == "STOP_AFTER_TP1":
            f1, f2 = tp1_exit_fraction, 1.0 - tp1_exit_fraction
            exit_notional_price = f1 * tp1 + f2 * exit_price
            # Realize BOTH legs: the configured fraction exits at TP1, the remainder at stop/gap fill.
            gross_pnl = qty * (f1 * (tp1 - entry if side == "LONG" else entry - tp1)
                               + f2 * (exit_price - entry if side == "LONG" else entry - exit_price))
        elif exit_type == "OPEN_AT_END" and tp1_seen:
            f1, f2 = tp1_exit_fraction, 1.0 - tp1_exit_fraction
            exit_notional_price = f1 * tp1 + f2 * exit_price
            gross_pnl = qty * (f1 * (tp1 - entry if side == "LONG" else entry - tp1)
                               + f2 * (exit_price - entry if side == "LONG" else entry - exit_price))
        else:
            exit_notional_price = exit_price
            gross_pnl = qty * ((exit_price - entry) if side == "LONG" else (entry - exit_price))
        exit_fee = exit_notional_price * qty * fee
        exit_slippage = exit_notional_price * qty * slippage_bps / 10_000
        funding_cost, funding_status = _funding_cost_until(
            events=funding_events, start_time=next_bar["timestamp"],
            end_time=h1.iloc[exit_index]["timestamp"] + pd.Timedelta(hours=1),
            h1=h1, side=side, base_quantity=qty,
            tp1_time=(h1.iloc[tp1_index]["timestamp"] + pd.Timedelta(hours=1)) if tp1_index is not None else None,
            tp1_exit_fraction=tp1_exit_fraction,
        )
        if funding_cost is None:
            raise ValueError(
                f"Cannot value funding for trade entered {next_bar['timestamp']}: {funding_status}. "
                "Net P&L was not calculated."
            )
        fees = entry_fee + exit_fee
        slippage = entry_slippage + exit_slippage
        net_pnl = gross_pnl - fees - slippage - float(funding_cost)
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
            "risk_limit_mode": risk_limits["risk_limit_mode"],
            "risk_tier_source": risk_limits["risk_tier_source"],
            "initial_margin_rate_used": initial_margin,
            "maintenance_margin_rate_used": maintenance,
            "tp1_exit_fraction": tp1_exit_fraction, "tp2_r_multiple": tp2_r_multiple,
            "notional_usdt": float(qty * entry), "estimated_initial_margin_usdt": float(plan.initial_margin_estimate_usdt),
            "risk_budget_usdt": risk_budget_usdt, "modeled_stop_loss_risk_usdt": risk_usdt,
            "exit_price": float(exit_price), "exit_type": exit_type,
            "gross_pnl_usdt": float(gross_pnl), "fees_usdt": float(fees),
            "slippage_usdt": float(slippage),
            "funding_usdt": float(funding_cost) if funding_cost is not None else None,
            "funding_status": funding_status,
            "funding_price_method": (
                "EVENT_FAIR_PRICE" if funding_status == "HISTORICAL_RATE_WITH_EVENT_FAIR_PRICE"
                else "LAST_COMPLETED_1H_CLOSE_PROXY" if funding_status == "HISTORICAL_RATE_WITH_HOURLY_CLOSE_PROXY"
                else "NO_SETTLEMENT_OR_UNAVAILABLE"
            ),
            "net_pnl_usdt": float(net_pnl), "net_r": float(net_r),
            "strategy_score": float(decision.score), "kronos_filter_used": bool(use_kronos_filter),
            "kronos_filter_result": forecast_decision,
        }
        if exit_type == "OPEN_AT_END":
            unresolved_positions.append({
                **trade, "status": "UNRESOLVED_MARK_TO_MARKET_NOT_REALIZED",
                "estimated_net_pnl_usdt": float(net_pnl),
            })
            if not equity_curve or equity_curve[-1]["time"] != (h1.iloc[exit_index]["timestamp"] + pd.Timedelta(hours=1)).isoformat():
                equity_curve.append({"time": (h1.iloc[exit_index]["timestamp"] + pd.Timedelta(hours=1)).isoformat(),
                                     "equity": float(equity + net_pnl), "kind": "UNREALIZED_END_MARK"})
            break
        trades.append(trade)
        equity += net_pnl
        equity_curve.append({"time": (h1.iloc[exit_index]["timestamp"] + pd.Timedelta(hours=1)).isoformat(),
                             "equity": float(equity), "kind": "REALIZED"})
        i = max(i + 1, exit_index + 1)

    metrics = _metrics(trades, equity_curve, starting_equity, unresolved_positions)
    metrics["funding_data_status"] = "UNAVAILABLE_OR_FETCH_FAILED" if funding_events is None else "HISTORICAL_EVENTS_FETCHED_WITH_PER_TRADE_VALUATION"
    metrics["kronos_filter_used"] = bool(use_kronos_filter)
    metrics["kronos_pretraining_cutoff_verified"] = False if use_kronos_filter else None
    if use_kronos_filter:
        metrics["kronos_temporal_leakage_assessment"] = "POTENTIAL_PRETRAINING_OVERLAP_NOT_EXCLUDABLE"
        warnings.append(
            "Kronos pretraining-data cutoff is not independently verified; historical Kronos-filtered "
            "results may overlap the model's pretraining corpus and are exploratory, not leakage-cleared."
        )
    metrics["fee_source"] = str(contract.get("takerFeeRateSource") or "MEXC_CONTRACT_METADATA_CURRENT_SNAPSHOT")
    metrics["survivorship_bias_warning"] = "Current contract universe is not point-in-time historical membership."
    metrics["contract_metadata_point_in_time"] = False
    metrics["contract_metadata_warning"] = (
        "Backtest uses the contract metadata snapshot fetched at run time; historic tick size, fee tier, "
        "risk limits and leverage changes are not reconstructed."
    )
    metrics["portfolio_scope"] = "SINGLE_SYMBOL_ISOLATED_RESEARCH_SLEEVE_NOT_CROSS_MARGIN_PORTFOLIO"
    hash_builder = hashlib.sha256()
    for label, frame in (("1H", h1), ("4H", h4), ("1D", d1), ("12H", h12)):
        hash_builder.update(label.encode())
        hash_builder.update(pd.util.hash_pandas_object(frame, index=False).values.tobytes())
    hash_builder.update(json.dumps(contract, sort_keys=True, default=str).encode())
    hash_builder.update(json.dumps({
        "starting_equity": starting_equity, "risk_pct": risk_pct,
        "slippage_bps": slippage_bps, "min_net_rr": min_net_rr,
        "leverage": leverage, "margin_buffer_pct": margin_buffer_pct,
        "use_kronos_filter": use_kronos_filter,
        "tp1_exit_fraction": tp1_exit_fraction, "tp2_r_multiple": tp2_r_multiple,
        "funding_events": sorted(funding_events or [], key=lambda item: int(item.get("settleTime", 0))),
        "fee_rate": fee, "fee_source": contract.get("takerFeeRateSource", "MEXC_CONTRACT_METADATA_CURRENT_SNAPSHOT"),
    }, sort_keys=True).encode())
    hash_builder.update(json.dumps(
        [trade.get("kronos_filter_result") for trade in trades if trade.get("kronos_filter_result")],
        sort_keys=True, default=str,
    ).encode())
    return {
        "symbol": symbol, "start_time": start_ts.isoformat(), "end_time": end_ts.isoformat(),
        "input_data_sha256": hash_builder.hexdigest(),
        "strategy_version": "mexc-mtf-1d-12h-4h-1h-v1",
        "metrics": metrics, "trades": trades, "unresolved_positions": unresolved_positions,
        "equity_curve": equity_curve, "warnings": warnings,
    }
