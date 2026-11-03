from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.backtesting.engine import _metrics, run_event_backtest
from app.artifacts.store import DatabaseForecastArtifactStore
from app.config import get_settings
from app.db import SessionLocal
from app.forecasting.kronos import KronosForecastEngine
from app.forecasting.evaluation import evaluate_forecast_paths
from app.market.candles import aggregate_12h_from_4h
from app.market.mexc import MexcPublicClient, normalize_symbol
from app.market.universe import eligible_universe
from app.models import (BacktestReport, Forecast, ForecastEvaluation, FundingObservation,
                        Job, MarketSnapshot, OrderBookSnapshot, Signal, SystemEvent,
                        TradingCandidate)
from app.risk.manager import size_position
from app.services.jobs import enqueue_job, update_progress
from app.simulation.barrier import evaluate_paths
from app.simulation.bootstrap import moving_block_bootstrap_ohlc
from app.simulation.costs import net_reward_risk
from app.strategy.engine import analyze_multitimeframe

log = logging.getLogger(__name__)
_MODEL_ENGINE: KronosForecastEngine | None = None
_ARTIFACT_STORE = DatabaseForecastArtifactStore()


def _get_model_engine() -> KronosForecastEngine:
    global _MODEL_ENGINE
    if _MODEL_ENGINE is None:
        _MODEL_ENGINE = KronosForecastEngine()
    return _MODEL_ENGINE


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_bundle(client: MexcPublicClient, symbol: str, *, lookback_days: int = 200):
    end = int(_utc_now().timestamp())
    start = end - lookback_days * 86400
    d1 = client.klines(symbol, "Day1", start, end)
    h4 = client.klines(symbol, "Hour4", start, end)
    h1 = client.klines(symbol, "Min60", max(start, end - 2000 * 3600), end)
    h12 = aggregate_12h_from_4h(h4)
    return d1, h12, h4, h1


def _save_forecast(symbol: str, timeframe: str, input_end_time, output) -> str:
    blob = _ARTIFACT_STORE.encode(output.paths)
    forecast_id = None
    meta = dict(output.metadata)
    meta["shape"] = list(output.paths.shape)
    with SessionLocal.begin() as db:
        record = Forecast(
            symbol=symbol, timeframe=timeframe,
            model_id=meta["model_id"], model_revision=meta["model_revision"],
            tokenizer_revision=meta["tokenizer_revision"],
            input_end_time=pd.Timestamp(input_end_time).to_pydatetime(),
            sample_count=int(output.paths.shape[0]), horizon=int(output.paths.shape[1]),
            seed=int(meta["seed"]), summary=output.summary,
            paths_compressed=blob, metadata_json=meta,
        )
        db.add(record)
        db.flush()
        forecast_id = record.id
    return forecast_id


def _load_forecast_paths(record: Forecast) -> np.ndarray:
    expected = tuple(record.metadata_json["shape"])
    return _ARTIFACT_STORE.decode(record.paths_compressed, expected)



def _float_or_none(value):
    try:
        number = float(value)
        return number if np.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _persist_market_observations(symbol: str, ticker: dict, contract: dict,
                                 depth: dict, funding: dict) -> None:
    now = _utc_now()
    next_settle = funding.get("nextSettleTime")
    try:
        next_settle_dt = pd.to_datetime(int(next_settle), unit="ms", utc=True).to_pydatetime() if next_settle else None
    except (TypeError, ValueError, OverflowError):
        next_settle_dt = None
    depth_time = depth.get("timestamp")
    try:
        depth_ts = pd.to_datetime(int(depth_time), unit="ms", utc=True).to_pydatetime() if depth_time else now
    except (TypeError, ValueError, OverflowError):
        depth_ts = now
    bid = _float_or_none(ticker.get("bid1"))
    ask = _float_or_none(ticker.get("ask1"))
    spread = ((ask - bid) / ((ask + bid) / 2) * 10_000) if bid and ask and ask >= bid else None
    depth_payload = {key: depth.get(key) for key in ("bids", "asks", "version", "timestamp") if key in depth}
    with SessionLocal.begin() as db:
        db.add(MarketSnapshot(
            symbol=symbol, timestamp=now,
            last_price=_float_or_none(ticker.get("lastPrice")),
            mark_price=_float_or_none(ticker.get("fairPrice") or ticker.get("markPrice")),
            index_price=_float_or_none(ticker.get("indexPrice")),
            bid=bid, ask=ask,
            volume_24h=_float_or_none(ticker.get("volume24")),
            turnover_24h=_float_or_none(ticker.get("amount24")),
            open_interest_contracts=_float_or_none(ticker.get("holdVol")),
            funding_rate=_float_or_none(funding.get("fundingRate") or ticker.get("fundingRate")),
        ))
        db.add(FundingObservation(
            symbol=symbol, observed_at=now,
            funding_rate=_float_or_none(funding.get("fundingRate") or ticker.get("fundingRate")),
            next_settle_time=next_settle_dt,
            settlement_cycle_hours=None,
        ))
        db.add(OrderBookSnapshot(
            symbol=symbol, timestamp=depth_ts, best_bid=bid, best_ask=ask,
            spread_bps=spread, depth=depth_payload,
        ))


def _analysis_payload(symbol: str, d1: pd.DataFrame, h12: pd.DataFrame,
                      h4: pd.DataFrame, h1: pd.DataFrame, strategy: dict,
                      forecast: Forecast | None, forecast_race: dict | None,
                      historical_race: dict | None, risk_plan: dict | None,
                      market: dict, decision: str, reasons: list[str]) -> dict:
    return {
        "symbol": symbol,
        "as_of": _utc_now().isoformat(),
        "decision": decision,
        "reasons": reasons,
        "market": market,
        "timeframes": {
            "1D": {"bars": len(d1), "last_close": float(d1.iloc[-1]["close"]) if len(d1) else None},
            "12H": {"bars": len(h12), "last_close": float(h12.iloc[-1]["close"]) if len(h12) else None},
            "4H": {"bars": len(h4), "last_close": float(h4.iloc[-1]["close"]) if len(h4) else None},
            "1H": {"bars": len(h1), "last_close": float(h1.iloc[-1]["close"]) if len(h1) else None},
        },
        "strategy": strategy,
        "forecast": ({
            "id": forecast.id, "summary": forecast.summary, "metadata": forecast.metadata_json,
        } if forecast else {"status": "UNAVAILABLE"}),
        "kronos_tp_sl_race": forecast_race,
        "historical_scenario_race": historical_race,
        "risk_plan": risk_plan,
        "decision_is_research_only": True,
    }


def run_forecast_job(payload: dict) -> dict:
    symbol = normalize_symbol(payload.get("symbol", "BTC_USDT"))
    timeframe = payload.get("timeframe", "Min60")
    horizon = int(payload.get("horizon") or get_settings().forecast_horizon)
    samples = int(payload.get("samples") or get_settings().forecast_sample_count)
    if timeframe not in {"Min60", "Hour4", "Day1"}:
        raise ValueError("Forecast timeframe must be Min60, Hour4 or Day1")
    if not 1 <= samples <= 1000 or not 1 <= horizon <= 48:
        raise ValueError("Forecast samples must be 1..1000 and horizon 1..48")
    with MexcPublicClient() as client:
        interval_seconds = {"Min60": 3600, "Hour4": 14400, "Day1": 86400}[timeframe]
        end = int(_utc_now().timestamp())
        candles = client.klines(symbol, timeframe, end - 2000 * interval_seconds, end)
    engine = _get_model_engine()
    output = engine.forecast(candles, samples=samples, horizon=horizon, seed=payload.get("seed"))
    forecast_id = _save_forecast(symbol, timeframe, candles.iloc[-1]["timestamp"], output)
    return {"forecast_id": forecast_id, "symbol": symbol, "summary": output.summary,
            "metadata": output.metadata}


def _candidate_race(paths: np.ndarray, historical_paths: np.ndarray, side: str,
                    entry: float, stop: float, tp1: float, tp2: float,
                    fee_rate: float, slippage_bps: float) -> tuple[dict, dict]:
    return (
        evaluate_paths(paths, side, entry, stop, tp1, tp2, fee_rate=fee_rate, slippage_bps=slippage_bps),
        evaluate_paths(historical_paths, side, entry, stop, tp1, tp2, fee_rate=fee_rate, slippage_bps=slippage_bps),
    )


def run_analysis_job(job_id: str, payload: dict) -> dict:
    cfg = get_settings()
    symbol = normalize_symbol(payload.get("symbol", "BTC_USDT"))
    update_progress(job_id, 5, "Fetching current MEXC Futures market data")
    with MexcPublicClient() as client:
        d1, h12, h4, h1 = _fetch_bundle(client, symbol)
        contracts = client.contracts(symbol)
        tickers = client.tickers(symbol)
        depth = client.depth(symbol, 20)
        funding = client.funding_rate(symbol)
    if not contracts or not tickers or h1.empty:
        raise ValueError("MEXC did not return complete contract, ticker and candle data")
    contract, ticker = contracts[0], tickers[0]
    _persist_market_observations(symbol, ticker, contract, depth or {}, funding or {})
    update_progress(job_id, 15, "Evaluating completed-candle multi-timeframe strategy")
    strategy = analyze_multitimeframe(symbol, d1, h12, h4, h1).as_dict()
    decision = strategy["decision"]
    side = strategy["side"]
    forecast_record = None
    forecast_race = None
    historical_race = None
    risk_plan = None
    reasons = list(strategy["reasons"])
    if len(h1) < 64:
        raise ValueError("At least 64 completed 1H candles are required for Kronos")
    update_progress(job_id, 25, "Generating individual Kronos forecast paths")
    engine = _get_model_engine()
    output = engine.forecast(h1, samples=cfg.forecast_sample_count, horizon=cfg.forecast_horizon)
    forecast_id = _save_forecast(symbol, "Min60", h1.iloc[-1]["timestamp"], output)
    with SessionLocal() as db:
        forecast_record = db.get(Forecast, forecast_id)
        db.expunge(forecast_record)
    update_progress(job_id, 50, "Running 10,000 sequence-preserving historical scenarios")
    historical_paths = moving_block_bootstrap_ohlc(
        h1.tail(1000), n_paths=cfg.simulation_replays, horizon=cfg.forecast_horizon,
        block_length=cfg.simulation_block_length, seed=cfg.forecast_random_seed,
    )
    if side and strategy.get("entry") and strategy.get("stop") and strategy.get("tp1") and strategy.get("tp2"):
        fee = contract.get("takerFeeRate")
        fee_mode = contract.get("feeRateMode") or "NORMAL"
        fee_type = contract.get("feeRateType") or "BASE"
        if fee is None or fee_mode != "NORMAL" or fee_type != "BASE":
            reasons.append(
                f"Unsupported or missing contract fee schedule (mode={fee_mode}, type={fee_type}); "
                "net expectancy cannot be verified"
            )
            decision = "NO_TRADE"
        else:
            entry, stop = float(strategy["entry"]), float(strategy["stop"])
            tp1, tp2 = float(strategy["tp1"]), float(strategy["tp2"])
            forecast_race, historical_race = _candidate_race(
                output.paths, historical_paths, side, entry, stop, tp1, tp2,
                float(fee), cfg.backtest_slippage_bps,
            )
            path_share = output.summary["bullish_path_share"] if side == "LONG" else output.summary["bearish_path_share"]
            if path_share < 0.50:
                reasons.append("Kronos path distribution does not align with the strategy direction")
                decision = "WAIT"
            if historical_race["mean_net_r"] is None or historical_race["mean_net_r"] <= 0:
                reasons.append("Historical block-bootstrap expected net R is not positive")
                decision = "WAIT"
            bid, ask = float(ticker.get("bid1") or 0), float(ticker.get("ask1") or 0)
            spread_bps = ((ask - bid) / ((ask + bid) / 2) * 10_000) if bid > 0 and ask >= bid else 1e9
            if spread_bps > cfg.max_spread_bps:
                reasons.append(f"Spread {spread_bps:.2f} bps exceeds configured limit")
                decision = "NO_TRADE"
            if decision in {"LONG_CANDIDATE", "SHORT_CANDIDATE"}:
                risk = size_position(
                    equity=cfg.default_account_equity, risk_pct=cfg.risk_per_trade_pct,
                    entry=entry, stop=stop, side=side,
                    contract_size=float(contract.get("contractSize") or 0),
                    vol_unit=float(contract.get("volUnit") or 1), leverage=cfg.default_leverage,
                    taker_fee_rate=float(fee), slippage_bps=cfg.backtest_slippage_bps,
                    maintenance_margin_rate=(
                        float(contract["maintenanceMarginRate"])
                        if contract.get("maintenanceMarginRate") is not None
                        and contract.get("riskLimitMode") not in {"CUSTOM", "INCREASE"}
                        and not contract.get("riskLimitCustom")
                        else None
                    ),
                )
                risk_plan = risk.as_dict()
                geometry = net_reward_risk(
                    entry=entry, stop=stop, target=tp2, side=side,
                    contract_size=float(contract.get("contractSize") or 0),
                    contracts=float(risk.contracts), taker_fee_rate=float(fee),
                    slippage_bps=cfg.backtest_slippage_bps,
                )
                risk_plan["net_geometry"] = geometry
                if geometry["net_rr"] < cfg.min_net_rr:
                    decision = "NO_TRADE"
                    reasons.append("Net reward-to-risk is below configured minimum")
                else:
                    fingerprint = hashlib.sha256(
                        f"{symbol}|{side}|{h1.iloc[-1]['timestamp']}|{strategy.get('setup_4h')}".encode()
                    ).hexdigest()
                    with SessionLocal.begin() as db:
                        existing = db.scalar(select(Signal).where(Signal.fingerprint == fingerprint))
                        recent = db.scalar(select(Signal).where(
                            Signal.symbol == symbol,
                            Signal.created_at >= _utc_now() - timedelta(hours=2),
                            Signal.status.in_(["APPROVED_FOR_PAPER", "ACTIVE_PAPER"]),
                        ).limit(1))
                        active_rows = db.scalars(select(Signal).where(
                            Signal.status.in_(["APPROVED_FOR_PAPER", "ACTIVE_PAPER"]),
                            Signal.expires_at > _utc_now(),
                        )).all()
                        committed_risk = sum(
                            float((row.details or {}).get("risk_plan", {}).get("estimated_stop_loss_usdt") or 0)
                            for row in active_rows
                        )
                        proposed_risk = float(risk_plan["estimated_stop_loss_usdt"])
                        portfolio_risk_cap = cfg.default_account_equity * cfg.max_portfolio_risk_pct / 100
                        if committed_risk + proposed_risk > portfolio_risk_cap:
                            decision = "WAIT"
                            reasons.append(
                                f"Aggregate active paper stop-risk would exceed the configured portfolio cap "
                                f"({committed_risk + proposed_risk:.2f} > {portfolio_risk_cap:.2f} USDT)"
                            )
                        elif recent:
                            decision = "WAIT"
                            reasons.append("A recent unexpired paper signal already exists for this symbol")
                        elif existing:
                            decision = "WAIT"
                            reasons.append("This exact signal fingerprint has already been recorded")
                        else:
                            decision = "APPROVED_FOR_PAPER"
                            reasons.append("Research candidate passed strategy, forecast, historical-simulation and portfolio-risk checks")
                            db.add(Signal(
                                fingerprint=fingerprint, symbol=symbol, side=side,
                                status="APPROVED_FOR_PAPER", entry=entry, stop=stop,
                                tp1=tp1, tp2=tp2, score=float(strategy["score"]),
                                details={"analysis_job_id": job_id, "forecast_id": forecast_id,
                                         "forecast_path_share": path_share,
                                         "historical_mean_net_r": historical_race["mean_net_r"],
                                         "risk_plan": risk_plan},
                                expires_at=_utc_now() + timedelta(hours=2),
                            ))
                            db.add(SystemEvent(level="INFO", event_type="PAPER_SIGNAL_CREATED",
                                               message=f"{symbol} {side} research signal",
                                               details={"job_id": job_id, "forecast_id": forecast_id}))
    market = {
        "last_price": float(ticker.get("lastPrice") or 0),
        "bid": float(ticker.get("bid1") or 0), "ask": float(ticker.get("ask1") or 0),
        "turnover_24h": float(ticker.get("amount24") or 0),
        "open_interest_contracts": float(ticker.get("holdVol") or 0),
        "funding_rate": float(funding.get("fundingRate") or 0),
        "next_funding_time": funding.get("nextSettleTime"),
        "orderbook_timestamp": depth.get("timestamp"),
        "spread_bps": (((float(ticker.get("ask1") or 0) - float(ticker.get("bid1") or 0)) /
                         ((float(ticker.get("ask1") or 0) + float(ticker.get("bid1") or 0)) / 2) * 10_000)
                        if float(ticker.get("bid1") or 0) > 0 and float(ticker.get("ask1") or 0) >= float(ticker.get("bid1") or 0) else None),
        "contract_size": contract.get("contractSize"),
        "maker_fee_rate": contract.get("makerFeeRate"),
        "taker_fee_rate": contract.get("takerFeeRate"),
        "maintenance_margin_rate": contract.get("maintenanceMarginRate"),
    }
    candidate_fingerprint = hashlib.sha256(
        f"candidate|{symbol}|{h1.iloc[-1]['timestamp']}|{decision}|{strategy.get('setup_4h')}".encode()
    ).hexdigest()
    with SessionLocal.begin() as db:
        exists = db.scalar(select(TradingCandidate.id).where(TradingCandidate.fingerprint == candidate_fingerprint))
        if not exists:
            db.add(TradingCandidate(
                fingerprint=candidate_fingerprint, symbol=symbol, decision=decision,
                side=side, score=float(strategy.get("score") or 0), reasons=reasons,
                details={"strategy": strategy, "market": market,
                         "forecast_id": forecast_id, "historical_race": historical_race,
                         "forecast_race": forecast_race, "risk_plan": risk_plan},
            ))
    result = _analysis_payload(symbol, d1, h12, h4, h1, strategy, forecast_record,
                               forecast_race, historical_race, risk_plan, market,
                               decision, reasons)
    result["forecast"]["status"] = "GENERATED"
    result["forecast"]["id"] = forecast_id
    update_progress(job_id, 95, "Persisting analysis and paper research signal")
    return result



def run_forecast_evaluation_job(job_id: str, payload: dict) -> dict:
    forecast_id = str(payload.get("forecast_id", ""))
    with SessionLocal() as db:
        record = db.get(Forecast, forecast_id)
        if record is None:
            raise ValueError("Forecast not found")
        existing = db.scalar(select(ForecastEvaluation).where(ForecastEvaluation.forecast_id == forecast_id))
        if existing:
            return {"forecast_id": forecast_id, "status": "ALREADY_EVALUATED", "metrics": existing.metrics}
        db.expunge(record)
    interval_seconds = {"Min60": 3600, "Hour4": 14400, "Day1": 86400}.get(record.timeframe)
    if interval_seconds is None:
        raise ValueError(f"Unsupported forecast timeframe for evaluation: {record.timeframe}")
    prediction_times = record.metadata_json.get("prediction_timestamps") or []
    if len(prediction_times) != record.horizon:
        raise ValueError("Forecast metadata does not contain all prediction timestamps")
    expected = pd.to_datetime(prediction_times, utc=True)
    end_ts = expected[-1] + pd.Timedelta(seconds=interval_seconds)
    if pd.Timestamp.now(tz="UTC") < end_ts:
        return {"forecast_id": forecast_id, "status": "NOT_MATURED", "matures_at": end_ts.isoformat()}
    update_progress(job_id, 20, "Fetching realized MEXC candles for forecast evaluation")
    with MexcPublicClient() as client:
        actual = client.klines(
            record.symbol, record.timeframe,
            int(expected[0].timestamp()), int(end_ts.timestamp()),
        )
    actual = actual.loc[actual["timestamp"].isin(expected)].sort_values("timestamp")
    actual_times = pd.DatetimeIndex(pd.to_datetime(actual["timestamp"], utc=True))
    expected_times = pd.DatetimeIndex(expected)
    if len(actual) != record.horizon or not actual_times.equals(expected_times):
        raise ValueError("Actual forecast horizon is incomplete or misaligned; evaluation is deferred")
    paths = _load_forecast_paths(record)
    # Directional validation requires the last observed input close stored with the forecast.
    baseline = record.summary.get("baseline_close")
    if baseline is not None:
        metrics = evaluate_forecast_paths(paths, actual["close"].to_numpy(dtype=float), float(baseline))
    else:
        raise ValueError("Forecast summary is missing baseline_close; refusing directional scoring")
    update_progress(job_id, 85, "Persisting out-of-sample forecast evaluation")
    with SessionLocal.begin() as db:
        db.add(ForecastEvaluation(
            forecast_id=forecast_id, horizon=record.horizon,
            mae_close=metrics["mae_close"], rmse_close=metrics["rmse_close"],
            directional_accuracy=metrics["directional_accuracy_final"],
            p10_p90_coverage=metrics["p10_p90_coverage"],
            mean_p10_p90_width=metrics["mean_p10_p90_width"], metrics=metrics,
        ))
        live = db.get(Forecast, forecast_id)
        if live:
            metadata = dict(live.metadata_json or {})
            metadata["evaluation_status"] = "EVALUATED_OUT_OF_SAMPLE"
            live.metadata_json = metadata
    return {"forecast_id": forecast_id, "status": "EVALUATED_OUT_OF_SAMPLE", "metrics": metrics}


def _run_symbol_backtest(*, job_id: str, symbol: str, start: datetime, end: datetime,
                        starting_equity: float, use_kronos: bool,
                        model_engine: KronosForecastEngine | None) -> dict:
    cfg = get_settings()
    warm_start = start - timedelta(days=65)
    with MexcPublicClient() as client:
        contract_rows = client.contracts(symbol)
        d1 = client.klines(symbol, "Day1", int(warm_start.timestamp()), int(end.timestamp()))
        h4 = client.klines(symbol, "Hour4", int(warm_start.timestamp()), int(end.timestamp()))
        h1 = client.klines(symbol, "Min60", int(warm_start.timestamp()), int(end.timestamp()))
        try:
            funding_events = client.funding_history_range(
                symbol, int(start.timestamp() * 1000), int(end.timestamp() * 1000),
                page_size=1000, max_pages=10,
            )
        except Exception as exc:
            funding_events = []
            log.warning("Funding history unavailable/incomplete for %s: %s", symbol, exc)
    if not contract_rows:
        raise ValueError(f"MEXC contract metadata not found for {symbol}")
    contract = contract_rows[0]
    kronos_filter = None
    if use_kronos:
        if model_engine is None:
            raise RuntimeError("Kronos filter requested without a ready inference engine")
        def kronos_filter(prefix: pd.DataFrame, side: str) -> dict:
            if len(prefix) < 64:
                return {"approved": False, "reason": "not enough history"}
            forecast = model_engine.forecast(prefix, samples=cfg.forecast_sample_count, horizon=24)
            share = forecast.summary["bullish_path_share"] if side == "LONG" else forecast.summary["bearish_path_share"]
            return {"approved": share >= 0.50, "path_share": share, "calibration": "unvalidated"}
    result = run_event_backtest(
        symbol=symbol, candles_1h=h1, candles_4h=h4, candles_1d=d1,
        contract=contract, start_time=start, end_time=end,
        starting_equity=starting_equity,
        risk_pct=cfg.backtest_risk_per_trade_pct,
        slippage_bps=cfg.backtest_slippage_bps, min_net_rr=cfg.min_net_rr,
        use_kronos_filter=use_kronos, kronos_filter=kronos_filter,
        funding_events=funding_events,
    )
    for trade in result["trades"]:
        trade["symbol"] = symbol
    for position in result.get("unresolved_positions", []):
        position["symbol"] = symbol
    return result


def run_backtest_job(job_id: str, payload: dict) -> dict:
    cfg = get_settings()
    requested_symbol = str(payload.get("symbol", "BTC_USDT")).strip().upper()
    all_symbols = requested_symbol == "ALL"
    symbols: list[str]
    if all_symbols:
        with MexcPublicClient() as client:
            universe = eligible_universe(client)
        symbols = [x["symbol"] for x in universe[:cfg.universe_limit]]
        if not symbols:
            raise ValueError("MEXC returned no eligible contracts for ALL backtest")
    else:
        symbols = [normalize_symbol(requested_symbol)]
    days = int(payload.get("days", 30))
    if not 7 <= days <= cfg.backtest_max_days:
        raise ValueError(f"Backtest days must be between 7 and {cfg.backtest_max_days}")
    use_kronos = bool(payload.get("use_kronos_filter", False))
    end = _utc_now()
    start = end - timedelta(days=days)
    per_symbol_equity = cfg.backtest_default_equity / len(symbols)
    model_engine = _get_model_engine() if use_kronos else None
    results = []
    failures = []
    for index, symbol in enumerate(symbols, start=1):
        progress = 5 + 78 * (index - 1) / max(len(symbols), 1)
        update_progress(job_id, progress, f"Backtesting {symbol} ({index}/{len(symbols)})")
        try:
            result = _run_symbol_backtest(
                job_id=job_id, symbol=symbol, start=start, end=end,
                starting_equity=per_symbol_equity if all_symbols else cfg.backtest_default_equity,
                use_kronos=use_kronos, model_engine=model_engine,
            )
            results.append(result)
        except Exception as exc:
            failures.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"[:400]})
            log.exception("Backtest symbol failed: %s", symbol)
    if not results:
        raise ValueError(f"No symbol backtest completed; failures={failures[:5]}")

    trades = [trade for item in results for trade in item["trades"]]
    trades.sort(key=lambda item: item["exit_time"])
    unresolved = [position for item in results for position in item.get("unresolved_positions", [])]
    equity_curve = []
    equity = cfg.backtest_default_equity
    for trade in trades:
        equity += float(trade["net_pnl_usdt"])
        equity_curve.append({"time": trade["exit_time"], "equity": float(equity)})
    metrics = _metrics(trades, equity_curve, cfg.backtest_default_equity, unresolved)
    metrics.update({
        "symbols_requested": len(symbols),
        "symbols_tested": [item["symbol"] for item in results],
        "symbol_failures": failures,
        "equal_capital_sleeves": bool(all_symbols),
        "portfolio_model_warning": (
            "ALL uses equal starting-equity sleeves per tested symbol; it is not a full simultaneous "
            "cross-margin portfolio or historical account replay."
        ) if all_symbols else "Single-symbol backtest; portfolio interactions are not modeled.",
        "funding_data_status": "PER_TRADE_AVAILABILITY_RECORDED",
        "kronos_filter_used": use_kronos,
        "survivorship_bias_warning": "Current contract universe is not point-in-time historical membership.",
    })
    result = {
        "symbol": "ALL" if all_symbols else symbols[0],
        "start_time": start.isoformat(), "end_time": end.isoformat(),
        "metrics": metrics, "trades": trades, "unresolved_positions": unresolved,
        "equity_curve": equity_curve,
        "warnings": failures + [warning for item in results for warning in item.get("warnings", [])],
        "per_symbol": [{"symbol": item["symbol"], "metrics": item["metrics"]} for item in results],
    }
    update_progress(job_id, 90, "Saving verified backtest report")
    with SessionLocal.begin() as db:
        db.add(BacktestReport(
            job_id=job_id, symbol=result["symbol"],
            start_time=pd.Timestamp(start).to_pydatetime(), end_time=pd.Timestamp(end).to_pydatetime(),
            metrics=result["metrics"], trades=result["trades"],
            unresolved_positions=result["unresolved_positions"],
            equity_curve=result["equity_curve"], warnings=result["warnings"],
        ))
    return result


def run_market_scan(job_id: str, payload: dict | None = None) -> dict:
    cfg = get_settings()
    update_progress(job_id, 5, "Refreshing eligible MEXC USDT perpetual universe")
    with MexcPublicClient() as client:
        universe = eligible_universe(client)
    candidates = []
    rejected = []
    with MexcPublicClient() as client:
        for i, item in enumerate(universe):
            symbol = item["symbol"]
            try:
                d1, h12, h4, h1 = _fetch_bundle(client, symbol, lookback_days=200)
                strategy = analyze_multitimeframe(symbol, d1, h12, h4, h1).as_dict()
                if strategy["decision"] in {"LONG_CANDIDATE", "SHORT_CANDIDATE"}:
                    job = enqueue_job("analysis", {"symbol": symbol})
                    candidates.append({"symbol": symbol, "side": strategy["side"], "score": strategy["score"], "analysis_job_id": job.id})
                else:
                    rejected.append({"symbol": symbol, "decision": strategy["decision"],
                                     "reasons": strategy["reasons"][:2]})
            except Exception as exc:
                rejected.append({"symbol": symbol, "error": str(exc)[:240]})
            update_progress(job_id, 10 + (80 * (i + 1) / max(len(universe), 1)),
                            f"Scanned {i + 1}/{len(universe)} eligible contracts")
    now = _utc_now()
    with SessionLocal() as db:
        pending_forecasts = db.scalars(
            select(Forecast).outerjoin(ForecastEvaluation, ForecastEvaluation.forecast_id == Forecast.id)
            .where(ForecastEvaluation.id.is_(None)).order_by(Forecast.created_at.asc()).limit(200)
        ).all()
        evaluation_payloads = []
        for forecast in pending_forecasts:
            interval = {"Min60": 3600, "Hour4": 14400, "Day1": 86400}.get(forecast.timeframe)
            times = (forecast.metadata_json or {}).get("prediction_timestamps") or []
            if interval and times:
                maturity = pd.Timestamp(times[-1]) + pd.Timedelta(seconds=interval)
                if maturity.to_pydatetime() <= now:
                    evaluation_payloads.append(forecast.id)
    for forecast_id in evaluation_payloads:
        enqueue_job("evaluate_forecast", {"forecast_id": forecast_id})
    with SessionLocal.begin() as db:
        db.add(SystemEvent(level="INFO", event_type="MARKET_SCAN_COMPLETED",
                           message=f"Scanned {len(universe)} MEXC contracts; queued {len(candidates)} analysis jobs",
                           details={"candidates": len(candidates), "rejected": len(rejected)}))
    return {"universe_size": len(universe), "candidates": candidates,
            "rejected_count": len(rejected), "rejections": rejected[:100]}


def process_job(job: Job) -> dict:
    if job.job_type == "forecast":
        return run_forecast_job(job.payload or {})
    if job.job_type == "analysis":
        return run_analysis_job(job.id, job.payload or {})
    if job.job_type == "backtest":
        return run_backtest_job(job.id, job.payload or {})
    if job.job_type == "evaluate_forecast":
        return run_forecast_evaluation_job(job.id, job.payload or {})
    if job.job_type == "scan":
        return run_market_scan(job.id, job.payload or {})
    raise ValueError(f"Unknown job type: {job.job_type}")
