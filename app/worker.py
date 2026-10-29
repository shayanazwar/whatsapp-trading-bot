"""MEXC Futures all-contract scanner with optional simulated fills; no live orders."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import replace
from typing import Any, Mapping, Sequence

from app.config import load_settings
from app.market.candles import Candle
from app.mexc.client import MexcClient
from app.mexc.stream import ticker_updates
from app.paper_rsi_ema import PaperConfig, create_paper_position, resolve_paper_exit
from app.storage import Store
from app.strategy.rsi_ema import RsiEmaConfig, evaluate_signals

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mexc-rsi-ema-worker")


class SingleWorkerLock:
    """Postgres advisory lock held for the worker lifetime, avoiding duplicate schedulers."""
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.connection: Any = None

    def acquire(self) -> bool:
        if not self.database_url:
            return True  # Local development assumes a single manually started worker.
        try:
            import psycopg
            self.connection = psycopg.connect(
                self.database_url.replace("postgresql+psycopg://", "postgresql://", 1),
                connect_timeout=10, autocommit=True,
            )
            row = self.connection.execute("SELECT pg_try_advisory_lock(834752019)").fetchone()
            if not row or not bool(row[0]):
                self.connection.close()
                self.connection = None
                return False
            return True
        except Exception:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            raise

    def release(self) -> None:
        if self.connection is not None:
            try:
                self.connection.execute("SELECT pg_advisory_unlock(834752019)")
            finally:
                self.connection.close()
                self.connection = None


def _active_contract_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fail closed unless active status, sizing limits, and leverage are present."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or row.get("contractCode") or "").upper()
        if not symbol:
            continue
        try:
            # MEXC state 0 represents a normal/tradable contract; missing status is not proof.
            if row.get("state") is None or int(row["state"]) != 0:
                continue
            contract_size = float(row.get("contractSize", 0))
            vol_unit = float(row.get("volUnit", 0))
            min_vol = float(row.get("minVol", 0))
            max_vol = float(row.get("maxVol", 0))
            max_leverage = max(float(row.get("countryConfigContractMaxLeverage", 0)),
                               float(row.get("maxLeverage", 0)))
            if min(contract_size, vol_unit, min_vol, max_vol, max_leverage) <= 0:
                continue
            if min_vol > max_vol:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        out[symbol] = dict(row)
    return out


def _selected_price_series(bars: list[Candle], setting: str) -> list[Candle]:
    if setting != "real":
        return list(bars)
    if any(any(v is None for v in (bar.real_open, bar.real_high, bar.real_low, bar.real_close)) for bar in bars):
        raise ValueError("Configured real OHLC series missing; no silent price-series fallback")
    return [replace(bar, open=float(bar.real_open), high=float(bar.real_high),
                    low=float(bar.real_low), close=float(bar.real_close)) for bar in bars]


def _strategy_config(settings: Any) -> RsiEmaConfig:
    return RsiEmaConfig(
        rsi_period=settings.rsi_period, rsi_overbought=settings.rsi_overbought,
        ema_fast=settings.ema_fast, ema_mid=settings.ema_mid, ema_slow=settings.ema_slow,
        volume_period=settings.rsi_volume_period, volume_multiplier=settings.rsi_volume_multiplier,
    )


def _paper_equity(store: Store, starting_equity: float) -> float:
    closed = store.paper_positions(open_only=False)
    realized = sum(
        float(row.get("realised_pnl") or 0.0)
        for row in closed if row.get("mode") == "rsi_ema_paper" and row.get("status") == "closed"
    )
    return starting_equity + realized


async def _manage_open_paper_positions(store: Store, mexc: MexcClient, settings: Any, now: int) -> bool:
    """Manage existing simulated positions even if new-entry scanning is paused/stale."""
    closed_any = False
    for position in store.paper_positions(open_only=True):
        if position.get("mode") != "rsi_ema_paper":
            continue
        symbol = str(position["symbol"])
        entry_time = int(position["entry_time"])
        try:
            bars = await mexc.get_klines_window(
                symbol, "3M", start=entry_time, end=now, now_epoch=now,
                close_grace_seconds=settings.candle_close_grace_seconds, max_bars_per_request=1800,
            )
            bars = _selected_price_series(bars, settings.mexc_price_series)
        except Exception as exc:
            logger.error("Paper position management paused for %s: candle data unavailable (%s)", symbol, type(exc).__name__)
            store.append_event("RSI_EMA_PAPER_POSITION_MANAGEMENT_BLOCKED", {
                "symbol": symbol, "position_id": position["id"], "reason": "CANDLE_DATA_UNAVAILABLE",
                "error_type": type(exc).__name__, "new_entries_blocked": True,
            }, symbol=symbol, idempotency_key=f"paper-manage-missing:{position['id']}:{now // 180}")
            continue
        try:
            funding: Sequence[Mapping[str, Any]] | None = await mexc.funding_history_window(
                symbol, start=entry_time, end=now, max_pages=30,
            )
            # An empty history result after a full 8-hour interval is treated as unknown, not zero.
            if now - entry_time >= 8 * 3600 and not funding:
                funding = None
        except Exception as exc:
            logger.warning("Paper funding history unavailable for %s (%s); TP is held, protective exits remain active", symbol, type(exc).__name__)
            funding = None
        exit_row = resolve_paper_exit(position, bars, funding_events=funding)
        if exit_row is None:
            if funding is None and any(
                bar.complete and bar.open_time >= entry_time and bar.high >= float(position["target_price"])
                for bar in bars
            ):
                store.append_event("RSI_EMA_PAPER_TP_HELD_FUNDING_UNKNOWN", {
                    "position_id": position["id"], "symbol": symbol,
                    "reason": "Funding data unavailable; net-profit target cannot be verified",
                    "live_order_submitted": False,
                }, symbol=symbol, idempotency_key=f"paper-tp-funding-unknown:{position['id']}:{now // 180}")
            continue
        ok = store.close_paper_position(
            str(position["id"]), exit_time=int(exit_row["exit_time"]),
            exit_price=float(exit_row["exit_fill_price"]), realised_pnl=float(exit_row["net_pnl"]),
            close_reason=str(exit_row["reason"]),
        )
        if ok:
            closed_any = True
            event = {**exit_row, "position_id": position["id"], "symbol": symbol,
                     "mode": "PAPER_ONLY", "live_order_submitted": False}
            store.append_event("RSI_EMA_PAPER_EXIT", event, symbol=symbol,
                               idempotency_key=f"rsi-ema-paper-exit:{position['id']}")
            logger.info("Paper position closed %s %s net=%+.4f reason=%s", symbol,
                        position["id"], float(exit_row["net_pnl"]), exit_row["reason"])
    return closed_any


async def _run() -> None:
    settings = load_settings()
    store = Store(settings.database_url or settings.database_path)
    lock = SingleWorkerLock(settings.database_url)
    if not lock.acquire():
        raise RuntimeError("Another RSI/EMA worker holds the database advisory lock; refusing duplicate scan workers")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    tickers: dict[str, tuple[float, dict[str, Any]]] = {}
    stream_state = {"last_update": 0.0, "updates": 0}

    async def consume_tickers() -> None:
        async for ticker in ticker_updates(stop):
            symbol = str(ticker.get("symbol", "")).upper()
            if symbol:
                tickers[symbol] = (time.time(), ticker)
                stream_state["last_update"] = time.time()
                stream_state["updates"] += 1

    paper_cfg = PaperConfig(
        equity_start=100.0, margin_usdt=settings.initial_margin_usdt,
        max_leverage=settings.max_leverage, target_net_usdt=settings.net_take_profit_usdt,
        requested_stop_usdt=settings.requested_stop_loss_usdt,
        taker_fee_rate=settings.exchange_taker_fee_rate,
        slippage_bps=settings.backtest_slippage_bps_per_side,
    )
    strat_cfg = _strategy_config(settings)

    async with MexcClient(base_url=settings.mexc_rest_base_url, timeout_seconds=settings.http_timeout_seconds,
                          max_retries=settings.http_max_retries) as mexc:
        stream_task = asyncio.create_task(consume_tickers(), name="mexc-ticker-stream")
        try:
            store.set_state("rsi_ema_worker_status", "starting")
            logger.info("Worker started; live trading hard-disabled; strategy=3M RSI/EMA LONG-only; paper=%s",
                        settings.paper_trading_enabled)
            while not stop.is_set():
                try:
                    now = await mexc.server_time()
                except Exception as exc:
                    store.set_state("rsi_ema_worker_status", "waiting_for_exchange_time")
                    logger.error("Cannot establish MEXC server time; skipping cycle (%s)", type(exc).__name__)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=5)
                    except TimeoutError:
                        pass
                    continue
                current_boundary = (now // 180) * 180
                next_scan = (current_boundary + settings.candle_close_grace_seconds
                             if now < current_boundary + settings.candle_close_grace_seconds
                             else current_boundary + 180 + settings.candle_close_grace_seconds)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(1, next_scan - now))
                    break
                except TimeoutError:
                    pass
                if stop.is_set():
                    break
                try:
                    now = await mexc.server_time()
                except Exception as exc:
                    logger.error("Cannot revalidate MEXC time; no scan this cycle (%s)", type(exc).__name__)
                    continue

                had_open_position = bool(store.paper_positions(open_only=True))
                paper_closed_this_cycle = await _manage_open_paper_positions(store, mexc, settings, now)
                try:
                    contracts = _active_contract_rows(await mexc.contract_details())
                except Exception as exc:
                    contracts = {}
                    logger.error("Contract metadata unavailable; new signals/entries blocked (%s)", type(exc).__name__)

                stream_age = time.time() - float(stream_state["last_update"] or 0)
                if stream_age > 30 or not tickers:
                    store.append_event("RSI_EMA_SCAN_PAUSED", {
                        "reason": "STALE_TICKER_STREAM", "stream_age_seconds": stream_age,
                        "known_tickers": len(tickers), "existing_positions_still_managed": True,
                    }, idempotency_key=f"rsi-ema-paused:{now // 180}")
                    store.set_state("rsi_ema_worker_status", "stale_ticker_stream")
                    logger.warning("New entry scan skipped: ticker stream stale (age=%.1fs, symbols=%d); existing positions were still managed", stream_age, len(tickers))
                    continue

                active_symbols = [s for s in contracts if s in tickers and time.time() - tickers[s][0] <= 30]
                logger.info("3-minute completed-candle scan: eligible=%d streamed=%d", len(contracts), len(active_symbols))
                scanned = failed = found = 0
                candidate_signals: list[dict[str, Any]] = []

                async def scan_one(symbol: str) -> None:
                    nonlocal scanned, failed, found
                    start = now - (160 * 180)
                    try:
                        bars = await mexc.get_klines_window(
                            symbol, "3M", start=start, end=now, now_epoch=now,
                            close_grace_seconds=settings.candle_close_grace_seconds, max_bars_per_request=1800,
                        )
                        bars = _selected_price_series(bars, settings.mexc_price_series)
                        bars = [bar for bar in bars if bar.complete]
                        scanned += 1
                        if len(bars) < max(125, settings.ema_slow + 5):
                            return
                        signals = evaluate_signals(bars, strat_cfg)
                        latest = bars[-1]
                        signal_row = next((x for x in reversed(signals) if x.timestamp == latest.close_time), None)
                        if signal_row is None:
                            return
                        found += 1
                        _, ticker = tickers.get(symbol, (0, {}))
                        payload = {
                            "strategy": "RSI-EMA-3M-LONG-v1", "mode": "SIGNAL_ONLY",
                            "live_trading_enabled": False, "paper_trading_enabled": settings.paper_trading_enabled,
                            "signal": signal_row.as_dict(), "contract": contracts[symbol],
                            "ticker": {k: ticker.get(k) for k in ("lastPrice", "riseFallRate", "amount24", "volume24", "maxBidPrice", "minAskPrice")},
                            "rules": {"rsi_overbought": strat_cfg.rsi_overbought,
                                "ema_alignment": "EMA21 > EMA50 > EMA100", "global_position_limit": 1,
                                "entry_order_type": "simulated market only" if settings.paper_trading_enabled else "not submitted"},
                        }
                        store.append_event("RSI_EMA_SIGNAL_CANDIDATE", payload, symbol=symbol,
                            idempotency_key=f"rsi-ema-signal:{symbol}:{signal_row.timestamp}")
                        candidate_signals.append({"payload": payload, "symbol": symbol,
                            "signal": signal_row.as_dict(), "ticker": dict(ticker), "contract": contracts[symbol]})
                        logger.info("LONG candidate %s RSI=%.2f close=%.8g; real orders disabled", symbol, signal_row.rsi, latest.close)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed += 1
                        logger.debug("Symbol scan failed %s (%s)", symbol, type(exc).__name__)

                # Client-level request-family throttling serializes requests inside the documented rate ceiling.
                await asyncio.gather(*(scan_one(s) for s in active_symbols), return_exceptions=True)

                # Optional paper execution: only one global position, never open a new one in the
                # same cycle as closing an existing position. No private MEXC order is ever sent.
                if settings.paper_trading_enabled and not had_open_position and not paper_closed_this_cycle and candidate_signals:
                    candidate_signals.sort(key=lambda row: (
                        _safe_float(row["ticker"].get("amount24"), 0.0),
                        _safe_float(row["signal"].get("rsi"), 0.0),
                    ), reverse=True)
                    for candidate in candidate_signals:
                        symbol = candidate["symbol"]
                        ticker_at, ticker = tickers.get(symbol, (0.0, {}))
                        if time.time() - ticker_at > 30:
                            continue
                        if store.paper_positions(open_only=True):
                            break
                        signal_row = candidate["signal"]
                        candidate_id = f"RSI-EMA-3M:{symbol}:{signal_row['timestamp']}"
                        candidate_meta = {"candidate_id": candidate_id, "symbol": symbol, "side": "LONG",
                                          "decision_time": int(signal_row["timestamp"]), "signal": signal_row}
                        if not store.add_paper_intent(candidate_meta, expected_entry_time=int(signal_row["timestamp"]),
                                                      setup_key="RSI-EMA-3M-LONG-v1"):
                            continue
                        try:
                            equity = _paper_equity(store, paper_cfg.equity_start)
                            position = create_paper_position(
                                symbol=symbol, signal=signal_row, ticker=ticker, contract=candidate["contract"],
                                entry_time=int(signal_row["timestamp"]), equity=equity, config=paper_cfg,
                            )
                            position_id = store.open_paper_position(position)
                            store.update_paper_intent(candidate_id, status="filled_simulated", paper_position_id=position_id)
                            store.append_event("RSI_EMA_PAPER_OPENED", {
                                "position_id": position_id, "symbol": symbol, "direction": "LONG",
                                "entry_time": position["entry_time"], "entry_price_estimate": position["entry_price"],
                                "quantity": position["quantity"], "leverage": position["meta"]["leverage"],
                                "margin": position["meta"]["initial_margin"], "target_price": position["target_price"],
                                "stop_price": position["stop_price"], "liquidation_price_estimate": position["meta"]["liquidation_price_estimate"],
                                "live_order_submitted": False,
                            }, symbol=symbol, idempotency_key=f"rsi-ema-paper-open:{position_id}")
                            logger.info("PAPER LONG opened %s qty=%s leverage=%sx, simulated only", symbol,
                                        position["quantity"], position["meta"]["leverage"])
                            break
                        except Exception as exc:
                            store.update_paper_intent(candidate_id, status="rejected_simulated", rejection_reason=type(exc).__name__)
                            store.append_event("RSI_EMA_PAPER_ENTRY_REJECTED", {
                                "symbol": symbol, "candidate_id": candidate_id,
                                "reason": type(exc).__name__, "live_order_submitted": False,
                            }, symbol=symbol, idempotency_key=f"rsi-ema-paper-reject:{candidate_id}")
                            logger.info("Paper candidate rejected %s (%s)", symbol, type(exc).__name__)

                summary = {"timestamp": now, "eligible_contracts": len(contracts), "streamed_contracts": len(active_symbols),
                           "bars_scanned": scanned, "errors": failed, "signals": found,
                           "paper_trading_enabled": settings.paper_trading_enabled,
                           "live_orders_submitted": 0, "ticker_stream_updates": stream_state["updates"]}
                store.append_event("RSI_EMA_SCAN_SUMMARY", summary, idempotency_key=f"rsi-ema-scan:{now // 180}")
                store.set_state("rsi_ema_worker_status", "running")
                store.set_state("rsi_ema_worker_last_scan", str(now))
                logger.info("Scan complete: scanned=%d errors=%d candidates=%d paper=%s", scanned, failed, found,
                            settings.paper_trading_enabled)
        finally:
            stop.set()
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
            store.set_state("rsi_ema_worker_status", "stopped")
            lock.release()


def _safe_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed and abs(parsed) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
