"""Opt-in background worker for research alerts and local paper trading only.

Safety posture:
- Disabled unless SCHEDULER_ENABLED=true.
- Paper trading additionally requires PAPER_TRADING_ENABLED=true, an explicit
  DATA_ALIGNMENT_PROBE_APPROVED flag, and a successful alignment report.
- All orders remain local simulations. There is no exchange order client here.
- Crypto-related WhatsApp messages are never sent unless the separate policy gate
  is open and the operator has recorded written Meta clearance for the exact use.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.alerts import AlertService
from app.config import Settings
from app.commands import Command
from app.services.command_processor import CommandProcessor
from app.market.candles import Candle, aggregate_12h_from_4h
from app.mexc.client import MexcClient, MexcError
from app.paper import PaperBroker
from app.risk import RiskConfig, RiskEngine
from app.services.market_data import MarketDataService
from app.storage import Store
from app.strategy.rfpc import STRATEGY_VERSION, TradeCandidate, evaluate_strategy
from app.market.indicators import atr
from app.market.archive import CandleArchive
from app.services.universe import top_usdt_perpetual_symbols
from app.services.market_quality import assess_orderbook, adverse_funding_rate, net_reward_risk_after_costs
from app.backtest_rsi_ema import _round_trigger_to_tick
from app.whatsapp import WhatsAppCloud, sender_key

logger = logging.getLogger(__name__)


def _price(candle: Candle, field: str) -> float | None:
    if field == "real":
        values = (candle.real_open, candle.real_high, candle.real_low, candle.real_close)
        if any(value is None for value in values):
            return None
        # The caller picks the needed item from the raw fields; this method is used for opens/closes.
        return float(candle.real_open)
    return float(candle.open)


def _ohlc(candle: Candle, series: str) -> tuple[float, float, float, float] | None:
    if series == "real":
        values = (candle.real_open, candle.real_high, candle.real_low, candle.real_close)
        if any(value is None for value in values):
            return None
        return tuple(float(v) for v in values)  # type: ignore[return-value]
    return candle.open, candle.high, candle.low, candle.close


def _exit_reason_and_raw_price(position: Mapping[str, Any], candle: Candle, series: str) -> tuple[str, float] | None:
    values = _ohlc(candle, series)
    if values is None:
        return None
    op, hi, lo, _ = values
    side = str(position["side"])
    stop = float(position["stop_price"])
    target = float(position["target_price"])
    if side == "LONG":
        if op <= stop:
            return "STOP_GAP", min(op, stop)
        if op >= target:
            return "TARGET_GAP", target
        hit_stop = lo <= stop
        hit_target = hi >= target
    else:
        if op >= stop:
            return "STOP_GAP", max(op, stop)
        if op <= target:
            return "TARGET_GAP", target
        hit_stop = hi >= stop
        hit_target = lo <= target
    if hit_stop:
        return ("STOP_AMBIGUOUS_BAR" if hit_target else "STOP"), stop
    if hit_target:
        return "TARGET", target
    return None


class ResearchScheduler:
    """Scheduled alerts and forward paper simulation; never sends MEXC orders."""

    def __init__(self, settings: Settings, store: Store, mexc: MexcClient, whatsapp: WhatsAppCloud,
                 backtest_processor: CommandProcessor | None = None,
                 archive: CandleArchive | None = None) -> None:
        self.settings = settings
        self.store = store
        self.mexc = mexc
        self.whatsapp = whatsapp
        self.market_data = MarketDataService(
            mexc, close_grace_seconds=settings.candle_close_grace_seconds,
            price_series=settings.mexc_price_series,
        )
        self.alerts = AlertService(store)
        self.paper = PaperBroker(store)
        self.risk = RiskEngine(RiskConfig(
            risk_per_trade_fraction=settings.risk_per_trade_fraction,
            max_open_risk_fraction=settings.max_open_risk_fraction,
            max_concurrent_positions=settings.max_concurrent_positions,
            max_drawdown_fraction=settings.max_drawdown_fraction,
            max_daily_loss_fraction=settings.max_daily_loss_fraction,
            fee_rate_per_side=settings.exchange_taker_fee_rate,
            slippage_rate_per_side=settings.backtest_slippage_bps_per_side / 10000.0,
        ))
        self._stopped = asyncio.Event()
        self.archive = archive or CandleArchive(settings.market_data_dir)
        self.backtest_processor = backtest_processor
        self._backtest_task: asyncio.Task | None = None

    def alignment_gate_passed(self) -> tuple[bool, str]:
        if not self.settings.data_alignment_probe_approved:
            return False, "DATA_ALIGNMENT_PROBE_APPROVED is false"
        report_path = self.settings.reports_dir / "alignment_probe.json"
        if not report_path.is_file():
            return False, "reports/alignment_probe.json is missing"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, "alignment probe report is unreadable"
        if report.get("status") != "COMPLETE_WITH_OBSERVATIONS":
            return False, f"alignment probe status is {report.get('status', 'unknown')}"
        if report.get("alignment_gates_passed") is not True:
            return False, "alignment probe did not pass all deterministic checks"
        if report.get("price_series_semantics_reviewed") is not True:
            return False, "open/realOpen semantics have not been explicitly reviewed"
        requested = set(report.get("symbols_requested", []))
        observed = {symbol for symbol, result in report.get("symbols", {}).items() if result.get("status") == "observed"}
        if not requested or requested != observed:
            return False, "alignment probe did not observe every requested symbol"
        return True, "alignment probe and explicit configuration gate passed"

    async def run_forever(self) -> None:
        self.store.set_state("scheduler_status", "running")
        self.store.append_event("SCHEDULER_STARTED", {"interval_seconds": self.settings.scheduler_interval_seconds},
                                idempotency_key="scheduler-started")
        try:
            while not self._stopped.is_set():
                try:
                    await self.run_once()
                    self.store.set_state("scheduler_last_success", datetime.now(timezone.utc).isoformat())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Scheduled cycle failed (%s)", type(exc).__name__)
                    # Deduplicated by minute to avoid flooding the event store during a persistent outage.
                    minute_key = int(time.time() // 60)
                    self.store.append_event("SCHEDULER_CYCLE_FAILED", {"error_type": type(exc).__name__},
                                            idempotency_key=f"scheduler-error:{minute_key}")
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.settings.scheduler_interval_seconds)
                except TimeoutError:
                    pass
        finally:
            if self._backtest_task is not None and not self._backtest_task.done():
                try:
                    await asyncio.wait_for(self._backtest_task, timeout=5.0)
                except TimeoutError:
                    self._backtest_task.cancel()
                    await asyncio.gather(self._backtest_task, return_exceptions=True)
            self.store.set_state("scheduler_status", "stopped")

    def stop(self) -> None:
        self._stopped.set()

    async def run_once(self) -> None:
        """Single testable worker cycle. All data access stays on MEXC."""
        if self.backtest_processor is not None and (self._backtest_task is None or self._backtest_task.done()):
            self._backtest_task = asyncio.create_task(self._process_next_backtest_job(), name="backtest-job-runner")
        backtest_busy = self._backtest_task is not None and not self._backtest_task.done()
        if self.settings.mexc_universe_scan_enabled and not backtest_busy:
            await self._universe_scan_tick()
        elif self.settings.mexc_universe_scan_enabled and backtest_busy:
            self.store.set_state("scanner_pause_reason", "deferred while a durable backtest is using MEXC API capacity")
            logger.info("SWING SCAN DEFERRED | reason=backtest_job_active; avoid competing top-100 MEXC downloads")

        if self.settings.paper_trading_enabled:
            passed, reason = self.alignment_gate_passed()
            if not passed:
                self.store.set_state("paper_scheduler_gate", reason)
            else:
                self.store.set_state("paper_scheduler_gate", "open")
                await self._paper_tick()
        elif self.settings.data_alignment_probe_approved:
            passed, reason = self.alignment_gate_passed()
            self.store.set_state("paper_scheduler_gate", "paper-off" if passed else reason)

        # Never send any crypto-related messaging unless written policy clearance is recorded
        # and the explicit configuration gate is open.
        if self.settings.whatsapp_policy_gate_open and self.settings.whatsapp_allowed_senders:
            await self._alert_tick()
            await self._flush_outbox()
        else:
            self.store.set_state("whatsapp_dispatch_gate", "blocked; no crypto message sent")


    async def _process_next_backtest_job(self) -> None:
        """Claim one durable job and execute it away from the FastAPI web service."""
        if self.backtest_processor is None:
            return
        job = self.store.claim_next_backtest_job(f"research-worker-{id(self)}")
        if not job:
            return
        job_id = str(job["id"])
        args = dict(job.get("args") or {})
        args["_job_id"] = job_id
        try:
            self.store.update_backtest_job_progress(job_id, {
                "stage": "worker_claimed", "processed": 0, "total": 0, "percent": 0.0,
            })
            logger.info("BACKTEST JOB START | job_id=%s window=%s strategy=S1-RFPC worker=%s",
                        job_id, args.get("window"), f"research-worker-{id(self)}")
            result = await self.backtest_processor.execute(Command("BACKTEST", args))
            stored = {"text": result.text, "event_type": result.event_type}
            self.store.finish_backtest_job(job_id, result=stored)
            recipient = str(job.get("recipient") or "")
            if recipient and self.settings.whatsapp_policy_gate_open:
                self.store.enqueue_outbox(
                    recipient, result.text, category="crypto_research",
                    idempotency_key=f"backtest-job-result:{job_id}:{recipient}",
                )
                await self._flush_outbox()
                logger.info("BACKTEST JOB REPORT QUEUED FOR WHATSAPP | job_id=%s recipient_present=true", job_id)
            elif recipient:
                self.store.append_event(
                    "BACKTEST_JOB_REPORT_NOT_SENT_POLICY_GATE_CLOSED",
                    {"job_id": job_id, "recipient_present": True},
                    idempotency_key=f"backtest-report-policy-blocked:{job_id}",
                )
                logger.warning("BACKTEST JOB REPORT NOT SENT | job_id=%s reason=WHATSAPP_POLICY_GATE_CLOSED", job_id)
            self.store.append_event("BACKTEST_JOB_COMPLETED", {"job_id": job_id, "event_type": result.event_type},
                                    idempotency_key=f"backtest-job-complete:{job_id}")
            logger.info("BACKTEST JOB COMPLETE | job_id=%s response_chars=%d", job_id, len(result.text))
        except asyncio.CancelledError:
            self.store.finish_backtest_job(job_id, error="Worker shutdown cancelled the job")
            raise
        except Exception as exc:
            self.store.finish_backtest_job(job_id, error=f"{type(exc).__name__}: {str(exc)[:1600]}")
            self.store.append_event("BACKTEST_JOB_FAILED", {"job_id": job_id, "error_type": type(exc).__name__},
                                    idempotency_key=f"backtest-job-failed:{job_id}")
            logger.exception("BACKTEST JOB FAILED | job_id=%s", job_id)

    async def _universe_scan_tick(self) -> None:
        """Scan top USDT perpetuals once per completed 1H bar using S1-RFPC only."""
        started = time.perf_counter()
        self.store.set_state("top100_swing_scan_last_attempt", datetime.now(timezone.utc).isoformat())
        try:
            now = await self.mexc.server_time()
            slot = (now - self.settings.candle_close_grace_seconds) // 3600
            if self.store.get_state("top100_swing_scan_slot") == str(slot):
                return
            universe = await top_usdt_perpetual_symbols(self.mexc, self.settings.mexc_universe_top_n)
            funding_snapshot: dict[str, dict[str, Any]] = {}
            funding_snapshot_error: str | None = None
            try:
                funding_rows = await self.mexc.all_current_funding_rates()
                funding_snapshot = {
                    str(row.get("symbol", "")).upper(): row for row in funding_rows if row.get("symbol")
                }
            except Exception as exc:
                funding_snapshot_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                logger.warning("SWING SCAN FUNDING SNAPSHOT FAILED | error=%s", type(exc).__name__)

            semaphore = asyncio.Semaphore(4)
            total = len(universe)
            logger.info(
                "TOP-100 S1-RFPC SCAN START | slot=%d symbols=%d strategy_timeframes=1D,12H,4H,1H",
                slot, total,
            )

            async def scan_one(index: int, item: Mapping[str, Any]):
                symbol = str(item["symbol"])
                symbol_started = time.perf_counter()
                async with semaphore:
                    logger.info("SWING SYMBOL SCAN START | progress_index=%d/%d symbol=%s", index, total, symbol)
                    try:
                        data = await self.market_data.fetch_symbol(
                            symbol, window_days=2, daily_warmup_days=400,
                            extra_intraday_warmup_days=45, now_epoch=now,
                        )
                        h1 = [c for c in data.timeframes.get("1H", []) if c.complete]
                        if len(data.timeframes.get("1D", [])) < 200 or not h1:
                            return symbol, float(item["turnover_24h"]), "INSUFFICIENT_HISTORY", None, None, data
                        decision_time = h1[-1].close_time
                        evaluation = evaluate_strategy(
                            symbol, data.timeframes, decision_time=decision_time,
                        )
                        candidate = evaluation.candidate
                        if candidate is None:
                            rejection = ",".join(evaluation.rejection_reasons) or "NO_CANDIDATE"
                            return symbol, float(item["turnover_24h"]), "NO_SIGNAL", None, rejection, data

                        contract = data.contract or {}
                        funding = funding_snapshot.get(symbol.upper())
                        funding_rate: float | None = None
                        if funding is not None:
                            try:
                                funding_rate = float(funding.get("fundingRate"))
                            except (TypeError, ValueError, OverflowError):
                                funding_rate = None
                        adverse_rate = adverse_funding_rate(candidate.side, funding_rate) if funding_rate is not None else None

                        payload: dict[str, Any] = {
                            "symbol": symbol,
                            "direction": candidate.side,
                            "timestamp": int(candidate.decision_time),
                            "expires_at": int(candidate.decision_time) + self.settings.signal_ttl_bars * 3600,
                            "close": float(candidate.entry_reference),
                            "entry_reference": float(candidate.entry_reference),
                            "stop_price": float(candidate.stop_price),
                            "target_price": float(candidate.target_price),
                            "initial_risk_distance": float(candidate.initial_risk_distance),
                            "planned_price_rr": float(candidate.gross_reward_risk),
                            # The database's legacy score column is non-nullable. Zero is a
                            # sentinel, not a probability or confidence score; the RFPC strategy
                            # is rule-qualified and does not use a numeric score model.
                            "score": 0.0,
                            "grade": "RULE-QUALIFIED",
                            "strategy": STRATEGY_VERSION,
                            "strategy_timeframes": ["1D", "12H", "4H", "1H"],
                            "candidate_id": candidate.candidate_id,
                            "reason": candidate.reason,
                            "evidence": dict(candidate.evidence),
                            "funding_snapshot_available": funding is not None and funding_rate is not None,
                            "funding_snapshot_error": funding_snapshot_error,
                            "current_funding_rate": funding_rate,
                            "adverse_funding_rate": adverse_rate,
                            "turnover_24h": float(item["turnover_24h"]),
                        }

                        delivery_reasons: list[str] = []
                        try:
                            contract_size = float(contract.get("contractSize", contract.get("contract_size", 0.0)))
                            price_tick = float(contract.get("priceUnit", contract.get("price_unit", 0.0)))
                            if contract_size <= 0:
                                delivery_reasons.append("CONTRACT_SIZE_UNAVAILABLE")
                            if price_tick <= 0:
                                delivery_reasons.append("PRICE_TICK_UNAVAILABLE")
                            if price_tick > 0:
                                payload["stop_price"] = _round_trigger_to_tick(
                                    float(payload["stop_price"]), price_tick, candidate.side, "STOP"
                                )
                                payload["target_price"] = _round_trigger_to_tick(
                                    float(payload["target_price"]), price_tick, candidate.side, "TARGET"
                                )

                            fee_raw = contract.get("takerFeeRate", self.settings.exchange_taker_fee_rate)
                            try:
                                fee_rate = float(fee_raw)
                            except (TypeError, ValueError, OverflowError):
                                fee_rate = self.settings.exchange_taker_fee_rate
                            if not 0 <= fee_rate < 0.1:
                                fee_rate = self.settings.exchange_taker_fee_rate
                            cost_rr = net_reward_risk_after_costs(
                                direction=candidate.side,
                                entry_price=float(candidate.entry_reference),
                                stop_price=float(payload["stop_price"]),
                                target_price=float(payload["target_price"]),
                                fee_rate_per_side=fee_rate,
                                slippage_bps_per_side=self.settings.backtest_slippage_bps_per_side,
                            )
                            payload["cost_model"] = {
                                "fee_rate_per_side": fee_rate,
                                "slippage_bps_per_side": self.settings.backtest_slippage_bps_per_side,
                                "funding_excluded_from_planned_rr": True,
                                **cost_rr,
                            }
                            payload["planned_net_rr"] = cost_rr["net_rr"]
                            if cost_rr["net_rr"] < self.settings.candidate_min_net_rr:
                                delivery_reasons.append("NET_RR_BELOW_MINIMUM")

                            h1_values = data.timeframes.get("1H", [])
                            atr_values = atr(
                                [c.high for c in h1_values], [c.low for c in h1_values],
                                [c.close for c in h1_values], 14,
                            )
                            atr_value = float(atr_values[-1]) if atr_values and atr_values[-1] else 0.0
                            if contract_size > 0 and atr_value > 0:
                                depth = await self.mexc.depth(symbol, limit=20)
                                quality = assess_orderbook(
                                    depth, contract_size=contract_size, atr_value=atr_value,
                                    max_spread_atr_fraction=self.settings.live_max_spread_atr_fraction,
                                    min_depth_usdt=self.settings.live_min_orderbook_depth_usdt,
                                )
                                payload["orderbook_quality"] = quality
                                if not quality["passed"]:
                                    delivery_reasons.extend(quality["reasons"])
                            else:
                                payload["orderbook_quality"] = None
                                delivery_reasons.append("ORDERBOOK_CHECK_UNAVAILABLE")

                            if adverse_rate is None:
                                delivery_reasons.append("FUNDING_SNAPSHOT_UNAVAILABLE")
                            elif adverse_rate > self.settings.max_adverse_funding_rate:
                                delivery_reasons.append("ADVERSE_FUNDING_TOO_HIGH")
                        except Exception as exc:
                            payload["orderbook_quality"] = None
                            delivery_reasons.append(f"DELIVERY_VALIDATION_ERROR:{type(exc).__name__}")
                            logger.warning("SWING SIGNAL DELIVERY VALIDATION FAILED | symbol=%s error=%s", symbol, type(exc).__name__)

                        payload["delivery_block_reasons"] = list(dict.fromkeys(delivery_reasons))
                        payload["delivery_eligible"] = not delivery_reasons
                        logger.info(
                            "SWING SYMBOL CANDIDATE | progress_index=%d/%d symbol=%s side=%s delivery_eligible=%s net_rr=%s",
                            index, total, symbol, candidate.side, payload["delivery_eligible"],
                            payload.get("planned_net_rr"),
                        )
                        return symbol, float(item["turnover_24h"]), "SIGNAL", payload, None, data
                    except Exception as exc:
                        return symbol, float(item.get("turnover_24h", 0.0)), "DATA_ERROR", None, f"{type(exc).__name__}: {str(exc)[:140]}", None

            tasks = [asyncio.create_task(scan_one(index, item)) for index, item in enumerate(universe, 1)]
            rows: list[tuple[str, float, str, dict[str, Any] | None, str | None, Any]] = []
            processed = 0
            for task in asyncio.as_completed(tasks):
                symbol, turnover, status, signal, error, data = await task
                processed += 1
                rows.append((symbol, turnover, status, signal, error, data))
                logger.info(
                    "SWING SYMBOL SCAN | progress=%d/%d (%.1f%%) symbol=%s status=%s signals=%d",
                    processed, total, processed / total * 100 if total else 100,
                    symbol, status, sum(1 for row in rows if row[2] == "SIGNAL"),
                )
            rows.sort(key=lambda row: (-row[1], row[0]))
            signals = [row for row in rows if row[2] == "SIGNAL" and isinstance(row[3], dict)]
            errors = [row for row in rows if row[2] == "DATA_ERROR"]
            insufficient = sum(row[2] == "INSUFFICIENT_HISTORY" for row in rows)
            delivery_blocked = 0

            for symbol, turnover, status, signal, error, data in rows:
                bar_counts = {
                    tf: len(data.timeframes.get(tf, [])) for tf in ("1D", "12H", "4H", "1H")
                } if data is not None else {}
                payload = {
                    "symbol": symbol, "timeframe": "1H", "strategy": STRATEGY_VERSION,
                    "turnover_24h": turnover, "scan_slot": slot, "status": status,
                    "signal": signal, "error": error, "bar_counts": bar_counts,
                }
                if signal:
                    signal_version = str(signal.get("strategy") or STRATEGY_VERSION)
                    signal["signal_id"] = self.store.save_signal(signal, strategy_version=signal_version)
                    payload["signal"] = signal
                    if signal.get("delivery_eligible") is True:
                        if self.settings.signal_push_enabled and self.settings.whatsapp_policy_gate_open:
                            await self._queue_validated_signal(signal, turnover, slot)
                        else:
                            delivery_blocked += 1
                    else:
                        delivery_blocked += 1
                self.store.append_event("SCAN_RESULT_S1_RFPC", payload, symbol=symbol,
                    idempotency_key=f"scheduled-swing-scan:{symbol}:{slot}")

            success_count = sum(row[2] in {"SIGNAL", "NO_SIGNAL", "INSUFFICIENT_HISTORY"} for row in rows)
            summary = (
                f"discovered={len(universe)} processed={len(rows)} successful={success_count} "
                f"signals={len(signals)} delivery_blocked={delivery_blocked} "
                f"insufficient_history={insufficient} errors={len(errors)} "
                f"no_signal={sum(row[2] == 'NO_SIGNAL' for row in rows)}"
            )
            self.store.set_state("top100_swing_scan_slot", str(slot))
            if success_count > 0:
                self.store.set_state("top100_swing_scan_last_success", datetime.now(timezone.utc).isoformat())
                self.store.set_state("top100_swing_scan_last_error", "")
            else:
                self.store.set_state("top100_swing_scan_last_error", "No symbol returned valid completed-candle analysis")
            self.store.set_state("top100_swing_scan_summary", summary)
            self.store.append_event("TOP100_SWING_SCAN_CYCLE", {
                "scan_slot": slot, "symbols_discovered": len(universe),
                "symbols_processed": len(rows), "signals": len(signals),
                "data_errors": len(errors), "duration_seconds": round(time.perf_counter() - started, 3),
            }, idempotency_key=f"top100-swing-cycle:{slot}")
            logger.info("TOP-100 S1-RFPC SCAN COMPLETE | %s slot=%d duration_s=%.1f",
                        summary, slot, time.perf_counter() - started)
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:180]}"
            self.store.set_state("top100_swing_scan_last_error", error)
            self.store.append_event("TOP100_SWING_SCAN_FAILED", {"error": error},
                                    idempotency_key=f"top100-swing-failed:{int(time.time() // 3600)}")
            logger.exception("TOP-100 S1-RFPC SCAN FAILED | %s", error)

    async def _queue_validated_signal(self, signal: Mapping[str, Any], turnover: float, slot: int) -> None:
        """Queue fresh, liquidity/funding/cost-validated S1-RFPC research signals."""
        now = int(time.time())
        if signal.get("delivery_eligible") is not True:
            return
        expires_at = int(signal.get("expires_at", 0))
        if not expires_at or expires_at <= now:
            logger.info("SWING SIGNAL NOT QUEUED | symbol=%s reason=signal_expired", signal.get("symbol"))
            return
        symbol = str(signal.get("symbol", ""))
        direction = str(signal.get("direction", ""))
        strategy = str(signal.get("strategy", STRATEGY_VERSION))
        key = f"s1-rfpc-push:{symbol}:{direction}:{int(signal.get('timestamp', 0))}:{strategy}"
        body = (
            "📡 PAK TRADING ACADEMY — SWING RESEARCH SIGNAL\n"
            f"{symbol} · {direction} · 1D / 12H / 4H / 1H\n"
            f"Entry reference (1H close): {float(signal.get('entry_reference', signal.get('close', 0))):.10g}\n"
            f"Stop: {float(signal.get('stop_price', 0)):.10g} · Target: {float(signal.get('target_price', 0)):.10g}\n"
            f"Gross RR: {float(signal.get('planned_price_rr', 0)):.2f}R · Net RR after modeled fees/slippage: {float(signal.get('planned_net_rr', 0)):.2f}R\n"
            f"Valid until: {datetime.fromtimestamp(expires_at, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            "Completed-candle strategy candidate; order-book, funding and cost checks passed at scan time.\n"
            "Research alert only. No live order was placed; re-check current price and risk before acting."
        )
        queued = 0
        for recipient in dict.fromkeys(sender_key(x) for x in self.settings.whatsapp_allowed_senders if sender_key(x)):
            expiry_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
            self.store.enqueue_outbox(recipient, body, category="crypto_research",
                                      idempotency_key=f"{key}:{recipient}", expires_at=expiry_iso)
            queued += 1
        logger.info("SWING SIGNAL OUTBOX QUEUED | symbol=%s side=%s recipients=%d expiry=%s",
                    symbol, direction, queued, expires_at)

    async def _paper_tick(self) -> None:
        if self.store.get_state("entry_halt", "false") == "true":
            self.store.set_state("paper_scheduler_gate", "operator pause latch active")
            # Manage existing simulated positions below is intentionally still allowed.
        try:
            now = await self.mexc.server_time()
        except Exception as exc:
            self.store.set_state("paper_scheduler_gate", f"exchange clock unavailable: {type(exc).__name__}")
            return
        for symbol in self.settings.default_symbols:
            try:
                await self._paper_symbol_tick(symbol, now)
            except Exception as exc:
                logger.warning("Paper cycle for %s failed (%s)", symbol, type(exc).__name__)
                self.store.append_event("PAPER_SYMBOL_CYCLE_FAILED", {"error_type": type(exc).__name__},
                                        symbol=symbol, idempotency_key=f"paper-symbol-failed:{symbol}:{now // 3600}")

    async def _paper_symbol_tick(self, symbol: str, now: int) -> None:
        # Do not reload hundreds of days of warm-up data every scheduler minute. Strategy
        # decisions are hourly-close-driven; retry only until the latest eligible close is processed.
        last_processed_raw = self.store.get_state(f"paper-last-closed:{symbol}", "0") or "0"
        try:
            last_processed = int(last_processed_raw)
        except ValueError:
            last_processed = 0
        expected_closed_hour = ((now - self.settings.candle_close_grace_seconds) // 3600) * 3600
        if last_processed >= expected_closed_hour:
            return
        # Daily history initializes EMA200; intraday history supplies setup and trigger context.
        data = await self.market_data.fetch_symbol(symbol, window_days=2, daily_warmup_days=400,
                                                   extra_intraday_warmup_days=45, now_epoch=now)
        bars1 = data.timeframes["1H"]
        if not bars1:
            self.store.append_event("PAPER_DATA_REJECTED", {"reason": "NO_1H_DATA"}, symbol=symbol,
                                   idempotency_key=f"paper-no-1h:{symbol}:{now // 3600}")
            return
        latest = bars1[-1]
        new_bars = [c for c in bars1 if c.close_time > last_processed]
        positions = [p for p in self.store.paper_positions(open_only=True) if p["symbol"] == symbol]
        if positions and new_bars:
            await self._manage_positions(symbol, positions, new_bars, bars1, data.contract, now)
        if not new_bars:
            return

        # A data warning is a hard no-entry condition. Existing paper positions were still
        # processed conservatively from available bars above.
        data_clean = not data.warnings and data.contract is not None
        latest_decision_time = latest.close_time
        self.store.set_state(f"paper-last-closed:{symbol}", str(latest_decision_time))
        self.store.append_event("PAPER_BAR_PROCESSED", {
            "latest_closed_1h_open": latest.open_time, "latest_closed_1h_close": latest.close_time,
            "data_clean": data_clean, "warnings": list(data.warnings), "price_series": self.settings.mexc_price_series,
        }, symbol=symbol, idempotency_key=f"paper-bar:{symbol}:{latest.close_time}")
        if not data_clean:
            self.store.append_event("PAPER_ENTRY_BLOCKED", {"reason": "DATA_QUALITY_WARNINGS", "warnings": list(data.warnings)},
                                    symbol=symbol, idempotency_key=f"paper-data-veto:{symbol}:{latest.close_time}")
            return
        if self.store.get_state("entry_halt", "false") == "true":
            return
        if any(p["symbol"] == symbol for p in self.store.paper_positions(open_only=True)):
            return

        series = data.timeframes
        try:
            evaluation = evaluate_strategy(symbol, series, decision_time=latest_decision_time)
        except Exception as exc:
            self.store.append_event("PAPER_STRATEGY_REJECTED", {"reason": type(exc).__name__}, symbol=symbol,
                                    idempotency_key=f"paper-strategy-error:{symbol}:{latest_decision_time}")
            return
        if evaluation.candidate is None:
            reason = evaluation.rejection_reasons[0] if evaluation.rejection_reasons else "NO_CANDIDATE"
            self.store.append_event("PAPER_SIGNAL_REJECTED", {"reason": reason, "regime": evaluation.regime,
                "evidence": dict(evaluation.evidence)}, symbol=symbol,
                idempotency_key=f"paper-signal-reject:{symbol}:{latest_decision_time}")
            return

        candidate = evaluation.candidate
        # The next 1H open is a fill input only; its high/low/close are never used for the signal.
        current_rows = await self.mexc.get_klines(symbol, "1H", start=latest.close_time,
            end=now, now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds,
            include_incomplete=True)
        current = next((c for c in sorted(current_rows, key=lambda c: c.open_time)
                        if c.open_time == latest.close_time and not c.complete), None)
        if current is None:
            self.store.append_event("PAPER_ENTRY_SKIPPED", {"reason": "NEXT_1H_OPEN_UNAVAILABLE", "candidate_id": candidate.candidate_id},
                symbol=symbol, idempotency_key=f"paper-entry-no-open:{candidate.candidate_id}")
            return
        raw_open = current.real_open if self.settings.mexc_price_series == "real" else current.open
        if raw_open is None or float(raw_open) <= 0:
            self.store.append_event("PAPER_ENTRY_SKIPPED", {"reason": "CONFIGURED_OPEN_SERIES_UNAVAILABLE",
                "candidate_id": candidate.candidate_id}, symbol=symbol,
                idempotency_key=f"paper-entry-no-price:{candidate.candidate_id}")
            return
        slippage = self.settings.backtest_slippage_bps_per_side / 10000.0
        simulated_entry = float(raw_open) * (1 + slippage if candidate.side == "LONG" else 1 - slippage)
        stop = float(candidate.stop_price)
        if (candidate.side == "LONG" and stop >= simulated_entry) or (candidate.side == "SHORT" and stop <= simulated_entry):
            self.store.append_event("PAPER_ENTRY_REJECTED", {"reason": "STOP_INVALID_AFTER_OPEN_GAP", "candidate_id": candidate.candidate_id},
                                    symbol=symbol, idempotency_key=f"paper-open-gap-reject:{candidate.candidate_id}")
            return
        risk_distance = abs(simulated_entry - stop)
        target = simulated_entry + (2 * risk_distance if candidate.side == "LONG" else -2 * risk_distance)
        from dataclasses import replace
        sized_candidate = replace(candidate, entry_reference=simulated_entry, target_price=target,
                                  initial_risk_distance=risk_distance, gross_reward_risk=2.0)
        all_positions = self.store.paper_positions(open_only=True)
        closed_positions = self.store.paper_positions(open_only=False)
        realized = sum(float(p.get("realised_pnl") or 0.0) for p in closed_positions if p.get("status") == "closed")
        equity = max(0.0, self.settings.default_starting_balance + realized)
        open_for_risk = []
        for p in all_positions:
            meta = p.get("meta", {})
            open_for_risk.append({
                "notional": float(p["quantity"]) * float(meta.get("contract_size", 0)) * float(p["entry_price"]),
                "risk_amount": float(meta.get("modeled_stop_loss", meta.get("risk_budget", 0))),
            })
        risk_decision = self.risk.evaluate(sized_candidate, equity=equity, contract=data.contract,
            open_positions=open_for_risk, data_is_fresh=True, account_state_known=True,
            reconciliation_clean=True, halt_active=self.store.get_state("entry_halt", "false") == "true")
        self.store.append_event("PAPER_RISK_DECISION", {"candidate": sized_candidate.as_dict(),
            "risk": risk_decision.as_dict(), "simulated": True}, symbol=symbol,
            idempotency_key=f"paper-risk:{candidate.candidate_id}")
        if not risk_decision.approved:
            self.store.append_event("PAPER_ENTRY_REJECTED", {"candidate_id": candidate.candidate_id,
                "reason": risk_decision.reason, "risk": risk_decision.as_dict()}, symbol=symbol,
                idempotency_key=f"paper-risk-reject:{candidate.candidate_id}")
            return
        contract_size = float(data.contract.get("contractSize", 0))
        opening_fee = risk_decision.quantity * contract_size * simulated_entry * self.settings.exchange_taker_fee_rate
        position_id = self.paper.open_position(symbol=symbol, side=candidate.side, entry_time=current.open_time,
            entry_price=simulated_entry, stop_price=stop, target_price=target, quantity=risk_decision.quantity,
            candidate_id=candidate.candidate_id, meta={
                "strategy_version": candidate.strategy_version,
                "contract_size": contract_size,
                "opening_fee": opening_fee,
                "fee_rate": self.settings.exchange_taker_fee_rate,
                "slippage_rate": slippage,
                "risk_budget": risk_decision.risk_budget,
                "modeled_stop_loss": risk_decision.modeled_stop_loss,
                "notional": risk_decision.notional,
                "price_series": self.settings.mexc_price_series,
                "contract_snapshot": {k: data.contract.get(k) for k in ("contractSize", "priceUnit", "volUnit", "minVol", "maxVol", "state", "apiAllowed")},
                "data_timestamps": {tf: series[tf][-1].close_time for tf in ("1D", "12H", "4H", "1H") if series.get(tf)},
                "funding_costs_pending_until_exit": True,
            })
        self.store.append_event("PAPER_CANDIDATE_ACCEPTED", {"candidate_id": candidate.candidate_id,
            "position_id": position_id, "simulated_entry": simulated_entry, "stop": stop, "target": target,
            "risk": risk_decision.as_dict(), "simulated": True}, symbol=symbol,
            idempotency_key=f"paper-accepted:{candidate.candidate_id}")
        if self.settings.whatsapp_policy_gate_open and self.settings.whatsapp_allowed_senders:
            message = (f"[PAPER POSITION OPENED — SIMULATED] {symbol} {candidate.side}\n"
                      f"Entry: {simulated_entry:.10g} | SL: {stop:.10g} | TP: {target:.10g}\n"
                      f"Quantity: {risk_decision.quantity:g} contracts | risk budget: ${risk_decision.risk_budget:.2f}\n"
                      "Simulated public-data fill only. No MEXC order was sent.")
            await self._queue_to_allowlist(message, f"paper-open:{candidate.candidate_id}")

    async def _manage_positions(self, symbol: str, positions: Sequence[Mapping[str, Any]],
                                new_bars: Sequence[Candle], all_bars: Sequence[Candle],
                                contract: Mapping[str, Any] | None, now: int) -> None:
        series = self.settings.mexc_price_series
        ordered_bars = sorted(new_bars, key=lambda c: c.open_time)
        bar_by_time = {c.open_time: c for c in sorted(all_bars, key=lambda c: c.open_time)}
        for position in list(positions):
            meta = position.get("meta", {})
            entry_time = int(position["entry_time"])
            previous_open = entry_time - 3600
            closed = False
            for candle in ordered_bars:
                if candle.open_time < entry_time:
                    continue
                values = _ohlc(candle, series)
                if values is None:
                    self.store.append_event("PAPER_POSITION_DATA_UNAVAILABLE", {"position_id": position["id"]},
                        symbol=symbol, idempotency_key=f"paper-position-data-missing:{position['id']}:{candle.open_time}")
                    break
                op, hi, lo, _ = values
                # A gap while a simulated position is open is resolved at a conservative next observed price.
                if candle.open_time - previous_open > 3600:
                    stop = float(position["stop_price"])
                    raw_exit = min(op, stop) if position["side"] == "LONG" else max(op, stop)
                    await self._close_simulated(position, candle, raw_exit, "DATA_GAP_CONSERVATIVE", meta, now)
                    closed = True
                    break
                previous_open = candle.open_time
                max_hold = 240 * 3600
                if candle.open_time - entry_time >= max_hold:
                    await self._close_simulated(position, candle, op, "TIME_STOP", meta, now)
                    closed = True
                    break
                exit_result = _exit_reason_and_raw_price(position, candle, series)
                if exit_result:
                    reason, raw_exit = exit_result
                    await self._close_simulated(position, candle, raw_exit, reason, meta, now)
                    closed = True
                    break
            if closed:
                continue
            # A hole in the newest loaded history means no current protective simulation can be trusted.
            if contract is None:
                self.store.append_event("PAPER_POSITION_REVIEW_REQUIRED", {"position_id": position["id"], "reason": "CONTRACT_METADATA_MISSING"},
                    symbol=symbol, idempotency_key=f"paper-position-review:{position['id']}:{now // 3600}")

    async def _close_simulated(self, position: Mapping[str, Any], candle: Candle, raw_exit: float,
                               reason: str, meta: Mapping[str, Any], now: int) -> None:
        series = self.settings.mexc_price_series
        values = _ohlc(candle, series)
        if values is None:
            return
        side = str(position["side"])
        slippage = float(meta.get("slippage_rate", self.settings.backtest_slippage_bps_per_side / 10000.0))
        exit_price = raw_exit * (1 - slippage if side == "LONG" else 1 + slippage)
        qty = float(position["quantity"])
        contract_size = float(meta.get("contract_size", 0))
        if contract_size <= 0:
            self.store.append_event("PAPER_POSITION_REVIEW_REQUIRED", {"position_id": position["id"], "reason": "CONTRACT_SIZE_MISSING"},
                symbol=position["symbol"], idempotency_key=f"paper-close-review:{position['id']}")
            return
        entry_price = float(position["entry_price"])
        fee_rate = float(meta.get("fee_rate", self.settings.exchange_taker_fee_rate))
        opening_fee = float(meta.get("opening_fee", qty * contract_size * entry_price * fee_rate))
        closing_fee = qty * contract_size * exit_price * fee_rate
        direction = 1.0 if side == "LONG" else -1.0
        gross_pnl = qty * contract_size * (exit_price - entry_price) * direction
        funding_pnl = 0.0
        funding_complete = True
        try:
            events = await self.mexc.funding_history_window(position["symbol"], start=entry_time_safe(position),
                                                            end=candle.open_time, max_pages=5)
            entry_notional = qty * contract_size * entry_price
            for event in events:
                settle = int(event.get("settleTime", 0))
                rate = float(event.get("fundingRate", 0.0))
                pnl_at_settle = -(1.0 if side == "LONG" else -1.0) * rate * entry_notional
                if int(position["entry_time"]) < settle <= candle.open_time:
                    funding_pnl += pnl_at_settle
                elif candle.open_time < settle <= candle.close_time and pnl_at_settle < 0:
                    # If the exit occurred inside this candle, event ordering is unknowable. Charge
                    # adverse funding but do not award an ambiguous favourable settlement.
                    funding_pnl += pnl_at_settle
        except Exception as exc:
            funding_complete = False
            self.store.append_event("PAPER_FUNDING_HISTORY_UNAVAILABLE", {"position_id": position["id"], "error_type": type(exc).__name__},
                symbol=position["symbol"], idempotency_key=f"paper-funding-unavailable:{position['id']}")
        pnl = gross_pnl - opening_fee - closing_fee + funding_pnl
        succeeded = self.paper.close_position(str(position["id"]), exit_time=candle.open_time,
            exit_price=exit_price, close_reason=reason, realised_pnl=pnl)
        if succeeded:
            self.store.append_event("PAPER_PNL_COMPONENTS_SIMULATED", {
                "position_id": position["id"], "gross_pnl": gross_pnl, "opening_fee": opening_fee,
                "closing_fee": closing_fee, "funding_pnl": funding_pnl, "net_pnl": pnl,
                "funding_history_complete": funding_complete, "exit_reason": reason,
                "exit_price_includes_adverse_slippage": True, "simulated": True,
            }, symbol=position["symbol"], idempotency_key=f"paper-pnl-components:{position['id']}")
            if self.settings.whatsapp_policy_gate_open and self.settings.whatsapp_allowed_senders:
                await self._queue_to_allowlist(
                    f"[PAPER RESULT — SIMULATED] {position['symbol']} {side}\nExit: {exit_price:.10g} ({reason})\n"
                    f"Gross: ${gross_pnl:.4f} | fees: ${opening_fee + closing_fee:.4f} | funding: ${funding_pnl:.4f}\n"
                    f"Net simulated P&L: ${pnl:.4f}. Funding history complete: {funding_complete}. No MEXC order was sent.",
                    f"paper-close:{position['id']}")

    async def _alert_tick(self) -> None:
        active = self.alerts.list(active_only=True)
        if not active:
            return
        try:
            now = await self.mexc.server_time()
        except Exception as exc:
            self.store.set_state("alert_scheduler_status", f"clock unavailable: {type(exc).__name__}")
            return
        symbols = sorted({a["symbol"] for a in active})
        recipients = list(dict.fromkeys(sender_key(x) for x in self.settings.whatsapp_allowed_senders if sender_key(x)))
        if not recipients:
            return
        # Single-owner use is supported directly; alert state transitions are calculated once
        # and notifications are durably enqueued for every allowlisted recipient.
        seconds_by_interval = {"1H": 3600, "4H": 4 * 3600, "12H": 12 * 3600, "1D": 86400}
        for symbol in symbols:
            kinds = [a for a in active if a["symbol"] == symbol]
            intervals = {a["params"].get("interval", "1H") for a in kinds if a["kind"] in {"percent_change", "rsi_cross"}}
            due_intervals = set()
            for tf in intervals:
                due_close = ((now - self.settings.candle_close_grace_seconds) // seconds_by_interval[tf]) * seconds_by_interval[tf]
                prior_raw = self.store.get_state(f"alert-eval-close:{symbol}:{tf}", "0") or "0"
                try:
                    prior_close = int(prior_raw)
                except ValueError:
                    prior_close = 0
                if due_close > prior_close:
                    due_intervals.add(tf)
            series: dict[str, list[Candle]] = {}
            try:
                if due_intervals & {"1H", "4H", "12H"}:
                    series["1H"] = await self.mexc.get_klines_window(symbol, "1H", start=now - 40 * 3600, end=now,
                        now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds, max_bars_per_request=100)
                if due_intervals & {"4H", "12H"}:
                    series["4H"] = await self.mexc.get_klines_window(symbol, "4H", start=now - 72 * 3600, end=now,
                        now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds, max_bars_per_request=100)
                if "1D" in due_intervals:
                    series["1D"] = await self.mexc.get_klines_window(symbol, "1D", start=now - 40 * 86400, end=now,
                        now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds, max_bars_per_request=100)
                if "12H" in due_intervals:
                    series["12H"] = aggregate_12h_from_4h(series.get("4H", []))
                if self.settings.mexc_price_series == "real":
                    from dataclasses import replace
                    for tf, bars in list(series.items()):
                        if any(any(v is None for v in (c.real_open, c.real_high, c.real_low, c.real_close)) for c in bars):
                            raise MexcError(f"Configured real OHLC is incomplete for {symbol} {tf}")
                        series[tf] = [replace(c, open=float(c.real_open), high=float(c.real_high),
                            low=float(c.real_low), close=float(c.real_close)) for c in bars]
                ticker = await self.mexc.ticker(symbol)
                latest_price = None
                for field in ("lastPrice", "last_price", "last"):
                    try:
                        value = float(ticker.get(field, 0))
                        if value > 0:
                            latest_price = value
                            break
                    except (TypeError, ValueError):
                        pass
                # Evaluate each alert transition once, then fan it out to every allowed recipient.
                self.alerts.evaluate_symbol(symbol, candles_by_interval=series,
                                            latest_price=latest_price, recipient=recipients)
                # Advance interval watermarks only from actual completed candles fetched.
                for tf in due_intervals:
                    actual = series.get(tf, [])
                    valid = [c for c in actual if c.complete]
                    if valid:
                        self.store.set_state(f"alert-eval-close:{symbol}:{tf}", str(max(c.close_time for c in valid)))
            except Exception as exc:
                self.store.append_event("ALERT_EVALUATION_FAILED", {"error_type": type(exc).__name__}, symbol=symbol,
                    idempotency_key=f"alert-eval-failed:{symbol}:{now // 3600}")

    async def _flush_outbox(self) -> None:
        if not self.settings.whatsapp_policy_gate_open:
            return
        counts = self.store.outbox_month_summary()
        if counts["total"] >= self.settings.whatsapp_monthly_message_cap:
            self.store.set_state("whatsapp_dispatch_gate", "monthly local message cap reached")
            return
        for item in self.store.pending_outbox(limit=50):
            if counts["total"] >= self.settings.whatsapp_monthly_message_cap:
                break
            try:
                result = await self.whatsapp.send_text(item["recipient"], item["body"])
                messages = result.get("messages", [])
                provider_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
                self.store.record_outbox_attempt(item["id"], accepted=True, provider_message_id=provider_id)
                self.store.append_event("WHATSAPP_MESSAGE_ACCEPTED_BY_META", {
                    "outbox_id": item["id"], "provider_message_id": provider_id,
                    "category": item["category"], "delivery_confirmed": False,
                }, idempotency_key=f"outbox-accepted:{item['id']}")
            except Exception as exc:
                # A transport timeout can occur after Meta accepted a message. Unknown outcomes
                # are not blindly retried, to avoid duplicates.
                self.store.mark_outbox_unknown(item["id"], type(exc).__name__)
                self.store.append_event("WHATSAPP_SEND_UNKNOWN", {"outbox_id": item["id"], "error_type": type(exc).__name__},
                    idempotency_key=f"outbox-unknown:{item['id']}")
            counts = self.store.outbox_month_summary()

    async def _queue_to_allowlist(self, body: str, key: str) -> None:
        if not self.settings.whatsapp_policy_gate_open:
            return
        for recipient in dict.fromkeys(sender_key(x) for x in self.settings.whatsapp_allowed_senders if sender_key(x)):
            self.store.enqueue_outbox(recipient, body, category="service", idempotency_key=f"{key}:{recipient}")


def entry_time_safe(position: Mapping[str, Any]) -> int:
    try:
        return int(position["entry_time"])
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("Paper position has invalid entry_time") from exc
