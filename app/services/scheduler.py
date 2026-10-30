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
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.alerts import AlertService
from app.config import Settings
from app.market.candles import Candle, aggregate_12h_from_4h
from app.mexc.client import MexcClient, MexcError
from app.paper import PaperBroker
from app.risk import RiskConfig, RiskEngine
from app.services.market_data import MarketDataService
from app.storage import Store
from app.strategy.rfpc import TradeCandidate, evaluate_strategy
from app.strategy.rsi_ema import RsiEmaConfig, evaluate_signals
from app.services.universe import top_usdt_perpetual_symbols
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

    def __init__(self, settings: Settings, store: Store, mexc: MexcClient, whatsapp: WhatsAppCloud) -> None:
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
            self.store.set_state("scheduler_status", "stopped")

    def stop(self) -> None:
        self._stopped.set()

    async def run_once(self) -> None:
        """Single testable worker cycle. All data access stays on MEXC."""
        if self.settings.mexc_universe_scan_enabled:
            await self._universe_scan_tick()

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

    async def _universe_scan_tick(self) -> None:
        """Scan active USDT perpetuals once per completed 3-minute slot."""
        try:
            now = await self.mexc.server_time()
            # A 3M bar becomes eligible only after its close-grace period has elapsed.
            slot = (now - self.settings.candle_close_grace_seconds) // 180
            if self.store.get_state("top100_3m_scan_slot") == str(slot):
                return
            universe = await top_usdt_perpetual_symbols(self.mexc, self.settings.mexc_universe_top_n)
            cfg = RsiEmaConfig(
                rsi_period=self.settings.rsi_period, rsi_overbought=self.settings.rsi_overbought,
                ema_fast=self.settings.ema_fast, ema_mid=self.settings.ema_mid,
                ema_slow=self.settings.ema_slow, volume_period=self.settings.rsi_volume_period,
                volume_multiplier=self.settings.rsi_volume_multiplier,
            )
            semaphore = asyncio.Semaphore(8)

            async def scan_one(item: Mapping[str, Any]):
                symbol = str(item["symbol"])
                async with semaphore:
                    bars = await self.mexc.get_klines_window(
                        symbol, "3M", start=now - 450 * 180, end=now,
                        now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds,
                    )
                    if self.settings.mexc_price_series == "real":
                        missing = [bar.open_time for bar in bars if any(
                            value is None for value in (bar.real_open, bar.real_high, bar.real_low, bar.real_close)
                        )]
                        if missing:
                            raise MexcError(
                                f"Configured real OHLC series is incomplete on {len(missing)} 3M candle(s)."
                            )
                        bars = [replace(bar, open=float(bar.real_open), high=float(bar.real_high),
                                        low=float(bar.real_low), close=float(bar.real_close)) for bar in bars]
                    signals = evaluate_signals(bars, cfg)
                    latest = signals[-1] if signals and bars and signals[-1].timestamp == bars[-1].close_time else None
                    return symbol, item["turnover_24h"], latest, len(bars)

            results = await asyncio.gather(*(scan_one(item) for item in universe), return_exceptions=True)
            errors = signal_count = 0
            for result in results:
                if isinstance(result, Exception):
                    errors += 1
                    continue
                symbol, turnover, signal, bar_count = result
                payload = {
                    "symbol": symbol, "timeframe": "3M", "turnover_24h": turnover,
                    "scan_slot": slot, "status": "SIGNAL" if signal else "NO_SIGNAL",
                    "signal": signal.as_dict() if signal else None, "bars": bar_count,
                }
                signal_count += int(signal is not None)
                self.store.append_event("SCAN_RESULT_3M", payload, symbol=symbol,
                    idempotency_key=f"scheduled-scan3m:{symbol}:{slot}")
            self.store.set_state("top100_3m_scan_slot", str(slot))
            self.store.set_state("top100_3m_scan_last_success", datetime.now(timezone.utc).isoformat())
            self.store.set_state("top100_3m_scan_summary",
                f"scanned={len(universe)-errors}/{len(universe)} signals={signal_count} errors={errors}")
            logger.info("TOP-100 3M SCAN COMPLETE | scanned=%d signals=%d errors=%d slot=%d",
                        len(universe)-errors, signal_count, errors, slot)
        except Exception as exc:
            logger.warning("TOP-100 3M SCAN FAILED | error_type=%s detail=%s",
                           type(exc).__name__, str(exc)[:180])

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
