"""Command dispatch for the official WhatsApp adapter; all processing is deterministic."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.alerts import AlertService
from app.analysis import analyze_series, format_analysis
from app.backtest import BacktestConfig, run_backtest
from app.backtest_rsi_ema import (RsiEmaBacktestConfig, format_backtest_whatsapp, run_backtest_rsi_ema, write_backtest_artifacts)
from app.strategy.rsi_ema import RsiEmaConfig, evaluate_signals
from app.strategy.mtf_tpr import MtfTprConfig, evaluate_mtf_tpr
from app.market.archive import CandleArchive
from app.services.universe import top_usdt_perpetual_symbols
from app.charting import render_candle_chart
from app.commands import Command, CommandError, HELP_TEXT, parse_command
from app.config import Settings
from app.market.candles import Candle, INTERVAL_SECONDS, aggregate_3m_from_1m, aggregate_12h_from_4h
from app.mexc.client import MexcClient, MexcError
from app.services.market_data import MarketDataService, SymbolData
from app.storage import Store, canonical_json
from app.symbols import normalize_symbol
from app.whatsapp import WhatsAppCloud, WhatsAppPolicyBlocked, sender_key

logger = logging.getLogger(__name__)


def _expected_completed_bars(start_exclusive: int, end_inclusive: int, step_seconds: int) -> int:
    """Count UTC-aligned bar close boundaries in (start_exclusive, end_inclusive]."""
    if step_seconds <= 0 or end_inclusive <= start_exclusive:
        return 0
    first_close = (int(start_exclusive) // step_seconds + 1) * step_seconds
    last_close = (int(end_inclusive) // step_seconds) * step_seconds
    return max(0, ((last_close - first_close) // step_seconds) + 1)


@dataclass(slots=True)
class CommandResult:
    text: str
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (path, image); documents are disabled
    event_type: str = "COMMAND_COMPLETED"
    idempotency_key: str | None = None


class CommandProcessor:
    def __init__(self, settings: Settings, store: Store, mexc: MexcClient,
                 whatsapp: WhatsAppCloud | None = None, *, backtest_inline: bool = False,
                 archive: CandleArchive | None = None) -> None:
        self.settings = settings
        self.store = store
        self.mexc = mexc
        self.whatsapp = whatsapp
        self.market_data = MarketDataService(mexc, close_grace_seconds=settings.candle_close_grace_seconds,
                                             price_series=settings.mexc_price_series)
        self.alerts = AlertService(store)
        self._backtest_lock = asyncio.Lock()
        self.backtest_inline = backtest_inline
        self.archive = archive or CandleArchive(settings.market_data_dir)
        self.settings.reports_dir.mkdir(parents=True, exist_ok=True)

    async def process_text(self, sender: str, text: str, *, received_timestamp: int | None = None,
                           request_id: str | None = None,
                           enforce_whatsapp_policy: bool = True) -> CommandResult:
        # Authorize before parsing to avoid exposing command grammar to unauthorized senders.
        allowed = {sender_key(x) for x in self.settings.whatsapp_allowed_senders}
        if enforce_whatsapp_policy and (not allowed or sender_key(sender) not in allowed):
            raise CommandError("Sender is not authorized")
        if received_timestamp is not None and int(time.time()) - received_timestamp > 300:
            raise CommandError("This message is too old to execute. Send the command again.")
        command = parse_command(text)
        if command.name == "BACKTEST":
            command.args["_recipient"] = sender_key(sender) if enforce_whatsapp_policy else ""
            command.args["_request_id"] = request_id or f"local-{time.time_ns()}"
        elif command.name in {"BACKTEST_STATUS", "BACKTEST_REPORT"}:
            command.args["_recipient"] = sender_key(sender) if enforce_whatsapp_policy else ""
        if command.name == "BACKTEST":
            logger.info(
                "BACKTEST COMMAND PARSED | raw=%r requested_window=%s symbol=%s",
                text.strip(), command.args.get("window"), command.args.get("symbol", "TOP_UNIVERSE"),
            )
        result = await self.execute(command)
        self.store.append_event("WHATSAPP_COMMAND_PROCESSED" if enforce_whatsapp_policy else "LOCAL_COMMAND_PROCESSED",
            {"sender": sender_key(sender) if enforce_whatsapp_policy else "authenticated-web-dashboard",
             "command": command.name, "args": command.args,
             "result_event": result.event_type, "mode": "PAPER/RESEARCH"},
            idempotency_key=result.idempotency_key)
        return result

    async def execute(self, command: Command) -> CommandResult:
        name, args = command.name, command.args
        if name == "HELP":
            return CommandResult(HELP_TEXT)
        if name == "STATUS":
            positions = self.store.paper_positions(open_only=True)
            return CommandResult(
                "SYSTEM STATUS\n"
                f"Environment: {self.settings.app_env}\n"
                f"Scheduler configured: {'ON' if self.settings.scheduler_enabled else 'OFF'}\n"
                f"Paper mode configured: {'ON' if self.settings.paper_trading_enabled else 'OFF'}\n"
                f"Alignment approval configured: {'YES' if self.settings.data_alignment_probe_approved else 'NO'}\n"
                f"Entry halt latch: {'HALTED' if self.store.get_state('entry_halt', 'false') == 'true' else 'ALLOWING'}\n"
                f"Auto trading: {'ON' if self.settings.auto_trading_enabled else 'OFF'}\n"
                f"Live execution: DISABLED (build lock={not self.settings.live_execution_build_released})\n"
                f"Open simulated positions: {len(positions)}\n"
                f"WhatsApp policy gate: {'OPEN' if self.settings.whatsapp_policy_gate_open else 'CLOSED'}\n"
                "No live MEXC order path is enabled."
            )
        if name == "PAPER":
            view = args.get("view", "SUMMARY")
            all_positions = self.store.paper_positions(open_only=False)
            open_positions = [p for p in all_positions if p.get("status") == "open"]
            closed_positions = [p for p in all_positions if p.get("status") == "closed"]
            if view == "SUMMARY":
                start_equity = float(self.settings.default_starting_balance)
                realised = sum(float(p.get("realised_pnl") or 0.0) for p in closed_positions)
                equity = start_equity + realised
                return CommandResult(
                    "LIVE PAPER TRADING — FAKE MONEY ONLY\n"
                    f"Starting wallet: ${start_equity:.2f}\n"
                    f"Realised closed-trade P&L: ${realised:+.2f}\n"
                    f"Estimated wallet/equity: ${equity:.2f}\n"
                    f"Open virtual positions: {len(open_positions)} | Closed virtual trades: {len(closed_positions)}\n"
                    "Equity excludes unrealised P&L on open positions; no real funds or live orders are involved."
                )
            if view == "OPEN":
                if not open_positions:
                    return CommandResult("PAPER OPEN — no current virtual positions.\nFake money only; no live orders.")
                rows = ["PAPER OPEN — VIRTUAL POSITIONS ONLY"]
                for position in open_positions[-20:]:
                    opened = datetime.fromtimestamp(int(position["entry_time"]), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    rows.append(
                        f"{position['id']} | {position['symbol']} {position['side']} | entry={float(position['entry_price']):.8g} "
                        f"SL={float(position['stop_price']):.8g} TP={float(position['target_price']):.8g} "
                        f"qty={float(position['quantity']):.8g} | {opened}"
                    )
                rows.append("Fake money only; unrealised P&L is not marked to market.")
                return CommandResult("\n".join(rows))
            if view == "HISTORY":
                recent = list(reversed(closed_positions[-10:]))
                if not recent:
                    return CommandResult("PAPER HISTORY — no closed virtual trades yet.")
                rows = ["PAPER HISTORY — RECENT CLOSED VIRTUAL TRADES"]
                for position in recent:
                    closed_at = position.get("exit_time")
                    when = datetime.fromtimestamp(int(closed_at), timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if closed_at else "time unavailable"
                    pnl = float(position.get("realised_pnl") or 0.0)
                    rows.append(
                        f"{position['symbol']} {position['side']} | entry={float(position['entry_price']):.8g} "
                        f"exit={float(position.get('exit_price') or 0.0):.8g} | P&L={pnl:+.4f} | {when}"
                    )
                return CommandResult("\n".join(rows) + "\nFake money only; not a live-trading record.")
        if name == "SEARCH":
            query = args["query"].upper()
            contracts = await self.mexc.contract_details()
            matches = []
            for contract in contracts:
                symbol_text = str(contract.get("symbol", "")).upper()
                base_text = str(contract.get("baseCoin", contract.get("base_coin", ""))).upper()
                quote_text = str(contract.get("quoteCoin", contract.get("quote_coin", ""))).upper()
                if query not in symbol_text and query not in base_text:
                    continue
                state = contract.get("state")
                active = state in (0, "0")
                matches.append((not active, symbol_text, quote_text, contract))
            matches.sort(key=lambda item: (item[0], item[1]))
            if not matches:
                return CommandResult(f"MEXC FUTURES SEARCH — {query}\nNo matching contract found in current metadata.")
            rows = [f"MEXC FUTURES SEARCH — {query}", f"Matches: {min(len(matches), 20)} of {len(matches)}"]
            for inactive, symbol_text, quote_text, contract in matches[:20]:
                state_text = "ACTIVE" if not inactive else f"NOT ACTIVE (state={contract.get('state', 'unknown')})"
                rows.append(f"{symbol_text} | {state_text}" + (f" | quote={quote_text}" if quote_text else ""))
            rows.append("Search results are contract metadata, not a trade recommendation.")
            return CommandResult("\n".join(rows), event_type="CONTRACT_SEARCH_COMPLETED")
        if name == "PRICE" or name == "MARKET":
            symbol = args["symbol"]
            ticker = await self.mexc.ticker(symbol)
            price = _first_number(ticker, "lastPrice", "last_price", "last")
            if price is None:
                raise MexcError("MEXC ticker did not provide a last-traded price; no substitute price field was used")
            change = _optional_float(ticker.get("riseFallRate"))
            high = _first_number(ticker, "high24Price", "high24", "high")
            low = _first_number(ticker, "lower24Price", "low24Price", "low")
            bid = _first_number(ticker, "bid1", "bidPrice")
            ask = _first_number(ticker, "ask1", "askPrice")
            rows = [f"💰 {symbol.replace('_', '/')}", f"${price:,.10g}"]
            if change is not None:
                rows.append(f"24H {change * 100:+.2f}%")
            if high is not None or low is not None:
                rows.append(f"H {high:,.8g}" if high is not None else "H n/a")
                rows[-1] += f"  ·  L {low:,.8g}" if low is not None else "  ·  L n/a"
            if bid is not None or ask is not None:
                rows.append(f"Bid {bid:,.8g}" if bid is not None else "Bid n/a")
                rows[-1] += f"  ·  Ask {ask:,.8g}" if ask is not None else "  ·  Ask n/a"
            return CommandResult("\n".join(rows), event_type="PRICE_QUOTE")
        if name == "CHART":
            symbol = args["symbol"]
            timeframe = args["timeframe"]
            now = int(time.time())
            count = 400
            interval = INTERVAL_SECONDS[timeframe]
            if timeframe == "12H":
                source_4h = await self.mexc.get_klines_window(
                    symbol, "4H", start=now - count * interval, end=now, now_epoch=now,
                    close_grace_seconds=self.settings.candle_close_grace_seconds,
                    max_bars_per_request=1800,
                )
                source_4h = self._apply_selected_price_series(source_4h)
                candles = aggregate_12h_from_4h(source_4h)[-count:]
            else:
                candles = await self.mexc.get_klines_window(
                    symbol, timeframe, start=now - count * interval, end=now,
                    now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds,
                    max_bars_per_request=1800,
                )
                candles = self._apply_selected_price_series(candles)[-count:]
            if not candles:
                raise MexcError(f"MEXC returned no completed {timeframe} candles for {symbol}")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = self.settings.reports_dir / f"chart_{symbol}_{timeframe}_{stamp}.png"
            render_candle_chart(candles, path, timeframe=timeframe)
            caption = f"📈 {symbol.replace('_', '/')} · {timeframe}"
            return CommandResult(caption, [(str(path), "image")], "CHART_RENDERED")
        if name == "ANALYZE":
            symbol = args["symbol"]
            data = await self.market_data.fetch_symbol(symbol, window_days=15, daily_warmup_days=400, extra_intraday_warmup_days=45)
            report = analyze_series(symbol, data.timeframes, data_warnings=data.warnings)
            analysis_text = format_analysis(report)
            return CommandResult(analysis_text, event_type="ANALYSIS_COMPLETED")
        if name == "SCAN":
            return await self._scan_top_100_3m()
        if name == "BACKTEST":
            if not self.backtest_inline:
                clean_args = {k: v for k, v in args.items() if not str(k).startswith("_")}
                job_id = self.store.enqueue_backtest_job(
                    str(args.get("_recipient", "")), clean_args,
                    idempotency_key=f"backtest-request:{args.get('_request_id') or time.time_ns()}",
                )
                self.store.append_event("BACKTEST_JOB_QUEUED", {
                    "job_id": job_id, "window": args.get("window"),
                    "strategy_scope": args.get("strategy_scope", "both"),
                }, idempotency_key=f"backtest-job-queued:{job_id}")
                return CommandResult(
                    f"🧪 BACKTEST JOB QUEUED\nJob: {job_id}\nPeriod: {args.get('window')}\nStrategy: {args.get('strategy_scope', 'both').upper()}\nThe dedicated research worker will process this job. Use BACKTEST REPORT {job_id} or BACKTEST STATUS. Live orders OFF.",
                    event_type="BACKTEST_JOB_QUEUED",
                )
            async with self._backtest_lock:
                return await self._run_rsi_ema_backtest(args)
        if name == "BACKTEST_STATUS":
            recipient = str(args.get("_recipient") or "") or None
            jobs = self.store.recent_backtest_jobs(8, recipient=recipient)
            if not jobs:
                return CommandResult("No backtest jobs found for this sender.")
            lines = ["BACKTEST JOBS"]
            for job in jobs:
                progress = job.get("progress") or {}
                detail = f" · {progress.get('stage', '')} {progress.get('processed', '')}/{progress.get('total', '')}" if progress else ""
                lines.append(f"{job['id']} | {job['args'].get('window', '?')} | {job['status']}{detail} | {job.get('created_at', '')}")
            lines.append("Use BACKTEST REPORT <JOB_ID> for a completed result.")
            return CommandResult("\n".join(lines))
        if name == "BACKTEST_REPORT":
            recipient = str(args.get("_recipient") or "") or None
            job = self.store.get_backtest_job(str(args["job_id"]), recipient=recipient)
            if not job:
                return CommandResult("Backtest job not found or not accessible to this sender.")
            result = job.get("result") or {}
            if result.get("text"):
                return CommandResult(str(result["text"]))
            progress = job.get("progress") or {}
            return CommandResult(
                f"Backtest job {job['id']} status: {job['status']}\n"
                f"Progress: {progress or 'not started'}\nError: {job.get('error') or 'none'}"
            )
        if name == "ALERT_CREATE":
            alert_id = self.alerts.create(args["symbol"], args)
            self.store.append_event("ALERT_CREATED", {"alert_id": alert_id, "args": args}, symbol=args["symbol"], idempotency_key=f"alert-created:{alert_id}")
            return CommandResult(f"ALERT CREATED — {alert_id}\n{args['symbol']} {args['kind']}\nThe alert is stored persistently.", event_type="ALERT_CREATED")
        if name == "ALERTS":
            alerts = self.alerts.list()
            if not alerts:
                return CommandResult("No alerts configured.")
            lines = [f"{a['id']} | {a['symbol']} | {a['kind']} | {'ACTIVE' if a['active'] else 'PAUSED'} | {canonical_json(a['params'])}" for a in alerts]
            return CommandResult("ALERTS\n" + "\n".join(lines[:50]))
        if name == "DELETE_ALL_ALERTS":
            alerts = self.alerts.list()
            deleted = sum(1 for alert in alerts if self.alerts.delete(alert["id"]))
            self.store.append_event("ALL_ALERTS_DELETED", {"deleted_count": deleted},
                                    idempotency_key=f"all-alerts-deleted:{int(time.time() // 60)}")
            return CommandResult(f"DELETE ALL — removed {deleted} alert(s).")
        if name == "ALERT_DELETE":
            changed = self.alerts.delete(args["alert_id"])
            return CommandResult(f"Alert {args['alert_id']}: {'deleted' if changed else 'not found'}.")
        if name in {"ALERT_PAUSE", "ALERT_RESUME"}:
            active = name == "ALERT_RESUME"
            changed = self.alerts.set_active(args["alert_id"], active)
            return CommandResult(f"Alert {args['alert_id']}: {'ACTIVE' if active else 'PAUSED'}" if changed else f"Alert {args['alert_id']} not found.")
        if name in {"SIGNALS", "PERFORMANCE", "PNL"}:
            if name == "SIGNALS":
                active = self.store.active_signals(now_epoch=int(time.time()), limit=50)
                if not active:
                    summary = self.store.get_state("top100_3m_scan_summary", "No completed scan summary yet.")
                    last = self.store.get_state("top100_3m_scan_last_success", "never")
                    return CommandResult(
                        "ACTIVE 3M RESEARCH SIGNALS\nNo unexpired signals currently available.\n"
                        f"Last successful scan: {last}\nScanner: {summary}"
                    )
                lines = [f"ACTIVE 3M RESEARCH SIGNALS — {len(active)}"]
                for row in active[:20]:
                    signal = row["payload"]
                    expiry = datetime.fromtimestamp(int(row["expires_at"]), timezone.utc).strftime("%H:%M:%S UTC")
                    delivery = "PUSH-ELIGIBLE" if signal.get("delivery_eligible") else "PULL-ONLY / DELIVERY BLOCKED"
                    reasons = ",".join(signal.get("delivery_block_reasons") or [])
                    lines.append(
                        f"{row['symbol']} {row['direction']} @ {float(signal.get('close', 0.0)):.8g} "
                        f"· Score {row['score']:.1f}/100 · Price RR {float(signal.get('planned_price_rr', 0.0)):.2f} "
                        f"· {delivery} · expires {expiry}"
                    )
                    if reasons:
                        lines.append(f"  Delivery checks: {reasons}")
                lines.append("Research only. Entries are not exchange orders; verify current price and risk.")
                return CommandResult("\n".join(lines))
            runs = self.store.research_runs(20)
            if not runs:
                return CommandResult("No backtest runs recorded yet.")
            lines = []
            for run in runs:
                result = run.get("result") or {}
                lines.append(f"{run['created_at']} | {run['mode']} | {run['status']} | window={result.get('window','n/a')} | trades={result.get('completed_trades','n/a')} | PnL={result.get('total_pnl','n/a')}")
            return CommandResult("BACKTEST HISTORY\n" + "\n".join(lines[:15]))
        if name in {"OPEN_POSITIONS", "POSITIONS"}:
            positions = self.store.paper_positions(open_only=True)
            if not positions:
                return CommandResult("No open simulated paper positions.")
            lines = [f"{p['id']} | {p['symbol']} {p['side']} | entry={p['entry_price']} | SL(sim)={p['stop_price']} | TP(sim)={p['target_price']} | qty(sim)={p['quantity']}" for p in positions]
            return CommandResult("OPEN PAPER POSITIONS — SIMULATED\n" + "\n".join(lines[:20]))
        if name == "RISK":
            return CommandResult(
                "RISK SETTINGS (PROVISIONAL)\n"
                f"Per-trade risk: {self.settings.risk_per_trade_fraction:.2%}\n"
                f"Max open risk: {self.settings.max_open_risk_fraction:.2%}\n"
                f"Daily loss halt: {self.settings.max_daily_loss_fraction:.2%}\n"
                f"Peak drawdown halt: {self.settings.max_drawdown_fraction:.2%}\n"
                f"Max concurrent positions: {self.settings.max_concurrent_positions}\n"
                "These are unvalidated research defaults, not a recommendation to trade."
            )
        if name == "COST":
            counts = self.store.outbox_month_summary()
            return CommandResult(
                "WHATSAPP OUTBOX — CURRENT UTC MONTH\n"
                f"Tracked messages: {counts['total']} | accepted by Meta: {counts['accepted']} | "
                f"delivery confirmed: {counts['delivered']} | failed: {counts['failed']} | "
                f"unknown outcome: {counts['unknown']} | pending: {counts['pending']}\n"
                "These are local counts, not a billing statement. Meta pricing and the recipient-country rate card must be checked separately."
            )
        if name == "PAUSE":
            self.store.set_state("entry_halt", "true")
            self.store.append_event("OPERATOR_PAUSE_APPLIED", {"source": "whatsapp_command", "scope": "new paper entries only"},
                idempotency_key=f"pause-request:{int(time.time() // 60)}")
            return CommandResult("PAUSE APPLIED: new paper entries are halted by the persistent entry latch. Existing simulated positions are not deleted. This cannot affect or enable live trading.")
        if name == "AUTOTRADE":
            action = args["action"]
            if action == "OFF":
                self.store.set_state("auto_trading_override", "off")
                self.store.append_event("AUTOTRADE_OFF_APPLIED", {"source": "whatsapp_command"},
                    idempotency_key=f"autotrade-off:{int(time.time() // 60)}")
                return CommandResult("Automatic trading override is OFF. Live execution remains build-locked and disabled.")
            return CommandResult(f"AUTO_TRADING_ENABLED={self.settings.auto_trading_enabled}\nLIVE_EXECUTION_ENABLED={self.settings.live_execution_enabled}\nLIVE_EXECUTION_BUILD_RELEASED={self.settings.live_execution_build_released}\nNo order-execution path is available in this release.")
        if name == "LIVE_STATUS":
            return CommandResult("LIVE EXECUTION: DISABLED AND BUILD-LOCKED. No live order has been sent by this app.")
        raise CommandError(f"Unsupported command: {name}")

    async def _load_top_universe(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await top_usdt_perpetual_symbols(self.mexc, limit=limit)
        if not rows:
            raise MexcError("MEXC returned no eligible active USDT perpetual contracts with valid 24h turnover.")
        return rows

    def _rsi_ema_config(self) -> RsiEmaConfig:
        return RsiEmaConfig(
            rsi_period=self.settings.rsi_period, rsi_overbought=self.settings.rsi_overbought,
            rsi_oversold=self.settings.rsi_oversold,
            ema_fast=self.settings.ema_fast, ema_mid=self.settings.ema_mid,
            ema_slow=self.settings.ema_slow, volume_period=self.settings.rsi_volume_period,
            volume_multiplier=self.settings.rsi_volume_multiplier,
        )

    async def _scan_top_100_3m(self) -> CommandResult:
        """Return the latest dedicated-worker scan; never start a competing web scan."""
        events = self.store.recent_events(250, "SCAN_RESULT_3M")
        if not events:
            return CommandResult(
                "TOP-100 SCANNER HAS NO STORED CYCLE YET.\n"
                "Check /healthz for scanner_enabled, worker heartbeat and last scan error."
            )
        decoded = [(event, json.loads(event["payload_json"])) for event in events]
        slots = [int(payload["scan_slot"]) for _, payload in decoded if payload.get("scan_slot") is not None]
        latest_slot = max(slots) if slots else None
        rows = [payload for _, payload in decoded if latest_slot is None or payload.get("scan_slot") == latest_slot]
        signals = [row for row in rows if row.get("status") == "SIGNAL" and isinstance(row.get("signal"), dict)]
        errors = sum(row.get("status") == "DATA_ERROR" for row in rows)
        insufficient = sum(row.get("status") == "INSUFFICIENT_HISTORY" for row in rows)
        lines = [
            "🔎 MEXC TOP-100 FUTURES SCANNER · 3M / 15M",
            f"Latest cycle: {len(rows)} symbols · signals {len(signals)} · data errors {errors} · insufficient history {insufficient}",
            f"Last successful scan: {self.store.get_state('top100_3m_scan_last_success', 'never')}",
        ]
        for row in sorted(signals, key=lambda x: float((x.get("signal") or {}).get("score", 0)), reverse=True)[:15]:
            signal = row["signal"]
            lines.append(
                f"{row.get('symbol')} {signal.get('direction')} @ {float(signal.get('close', 0)):.8g} "
                f"· score {float(signal.get('score', 0)):.1f} · grade {signal.get('grade', '?')}"
            )
        lines.append("SCAN displays the dedicated worker's latest cycle; it does not launch a duplicate scan from the web process.")
        return CommandResult("\n".join(lines), event_type="LATEST_SCAN_VIEWED")

    async def _run_rsi_ema_backtest(self, args: dict[str, Any]) -> CommandResult:
        """Run reproducible CONTROL/CANDIDATE simulations from the worker-local 1M archive."""
        started = time.perf_counter()
        days_map = {"1D": 1, "7D": 7, "30D": 30, "60D": 60, "90D": 90}
        window = str(args.get("window") or "").strip().upper()
        legacy_windows = args.get("windows")
        if not window and isinstance(legacy_windows, list) and len(legacy_windows) == 1:
            window = str(legacy_windows[0]).strip().upper()
        if window and isinstance(legacy_windows, list) and len(legacy_windows) == 1:
            legacy_window = str(legacy_windows[0]).strip().upper()
            if legacy_window != window:
                raise CommandError(f"Conflicting backtest periods received: {window} vs {legacy_window}")
        if window not in days_map:
            raise CommandError("Backtest period missing or invalid. Send BACKTEST 1D or BACKTEST 7D.")
        longest = days_map[window]
        if not args.get("symbol") and longest > 7:
            raise CommandError("Top-100 backtests support 1D/7D. Use BACKTEST BTCUSDT 30D for a longer single-symbol run.")
        scope = str(args.get("strategy_scope", "both")).lower()
        if scope not in {"control", "candidate", "both"}:
            raise CommandError("strategy_scope must be CONTROL, CANDIDATE or BOTH")

        now = await self.mexc.server_time()
        cutoff = now - longest * 86400
        # 3M EMA warm-up plus 250 completed 15M context bars for the candidate model.
        htf_warmup_seconds = self.settings.mtf_htf_warmup_bars * 900
        history_start = cutoff - max(450 * 180, htf_warmup_seconds + 20 * 900)
        if args.get("symbol"):
            contract = await self.mexc.contract_detail(args["symbol"])
            if not contract:
                raise CommandError("The requested symbol is not an active MEXC Futures contract.")
            universe = [{"symbol": args["symbol"], "turnover_24h": 0.0, "contract": contract}]
        else:
            universe = await self._load_top_universe(self.settings.mexc_universe_top_n)

        # Fetch each symbol sequentially. API pagination is bounded to <=1,800 1M bars/request;
        # worker-local compressed archive avoids downloading the same days on every run.
        loaded: dict[str, list[Candle]] = {}
        specs: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        coverage_by_symbol: dict[str, float] = {}
        expected_3m = _expected_completed_bars(cutoff, now - self.settings.candle_close_grace_seconds, 180)
        job_id = str(args.get("_job_id") or "")
        if job_id:
            self.store.update_backtest_job_progress(job_id, {
                "stage": "loading_historical_data", "processed": 0, "total": len(universe),
                "window": window, "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        for processed_count, item in enumerate(universe, start=1):
            symbol = str(item["symbol"])
            contract = dict(item.get("contract") or {})
            if not contract:
                contract = await self.mexc.contract_detail(symbol) or {}
            valid, reason = _validate_backtest_contract_spec(
                contract, margin_mode=self.settings.backtest_margin_mode,
            )
            if not valid:
                failures.append(f"{symbol}:{reason}")
                continue
            try:
                archived = await asyncio.to_thread(self.archive.load, symbol, history_start, now)
                first_1m_open = ((history_start + 59) // 60) * 60
                last_1m_open = ((now - self.settings.candle_close_grace_seconds - 60) // 60) * 60
                expected_1m = max(0, ((last_1m_open - first_1m_open) // 60) + 1) if last_1m_open >= first_1m_open else 0
                archive_coverage = min(100.0, len(archived) / expected_1m * 100.0)
                if archive_coverage < self.settings.backtest_min_coverage_pct:
                    fetched = await self.mexc.get_klines_window(
                        symbol, "1M", start=history_start, end=now, now_epoch=now,
                        close_grace_seconds=self.settings.candle_close_grace_seconds,
                        max_bars_per_request=1800,
                    )
                    await asyncio.to_thread(self.archive.append, fetched)
                    archived = await asyncio.to_thread(self.archive.load, symbol, history_start, now)
                bars = aggregate_3m_from_1m(archived)
                bars = [b for b in bars if b.complete]
                if self.settings.mexc_price_series == "real":
                    if any(any(v is None for v in (b.real_open, b.real_high, b.real_low, b.real_close)) for b in bars):
                        raise MexcError("Configured real OHLC series is incomplete")
                    bars = [replace(b, open=float(b.real_open), high=float(b.real_high), low=float(b.real_low), close=float(b.real_close)) for b in bars]
                in_window = [b for b in bars if cutoff < b.close_time <= now - self.settings.candle_close_grace_seconds]
                coverage = min(100.0, len(in_window) / expected_3m * 100.0)
                coverage_by_symbol[symbol] = coverage
                if coverage < self.settings.backtest_min_coverage_pct:
                    failures.append(f"{symbol}:INSUFFICIENT_3M_COVERAGE_{coverage:.1f}%")
                    continue
                if len(bars) < 300:
                    failures.append(f"{symbol}:INSUFFICIENT_WARMUP_{len(bars)}")
                    continue
                loaded[symbol] = bars
                specs[symbol] = contract
                logger.info("BACKTEST SYMBOL READY | symbol=%s 1m=%d 3m=%d coverage=%.2f%%",
                            symbol, len(archived), len(bars), coverage)
            except Exception as exc:
                failures.append(f"{symbol}:{type(exc).__name__}:{str(exc)[:100]}")
                logger.warning("BACKTEST SYMBOL LOAD FAILED | symbol=%s error=%s detail=%s",
                               symbol, type(exc).__name__, str(exc)[:140])
            if job_id and (processed_count % 5 == 0 or processed_count == len(universe)):
                self.store.update_backtest_job_progress(job_id, {
                    "stage": "loading_historical_data", "processed": processed_count,
                    "total": len(universe), "valid_symbols": len(loaded),
                    "failed_symbols": len(failures), "last_symbol": symbol,
                    "window": window, "updated_at": datetime.now(timezone.utc).isoformat(),
                })

        if not loaded:
            raise MexcError("No eligible symbol had complete 3M history. No shortened or fabricated report was created. "
                            f"Failures: {', '.join(failures[:8])}")
        universe_coverage_pct = len(loaded) / len(universe) * 100.0 if universe else 0.0
        if not args.get("symbol") and universe_coverage_pct < self.settings.backtest_min_universe_coverage_pct:
            raise MexcError(
                "Insufficient top-universe data coverage: "
                f"{len(loaded)}/{len(universe)} symbols ({universe_coverage_pct:.1f}%), "
                f"minimum required {self.settings.backtest_min_universe_coverage_pct:.1f}%. "
                "No partial-universe performance report was generated. "
                f"Failures: {', '.join(failures[:10])}"
            )

        # Same 1M archive is used for signal bars and intrabar resolution source. The simulator
        # receives 3M bars; when TP/SL are both touched in one 3M candle it remains conservative
        # (SL-first), and that assumption is disclosed in the report.
        control_strategy = self._rsi_ema_config()
        candidate_strategy = MtfTprConfig(
            min_score=self.settings.signal_min_score,
            stop_atr_multiple=self.settings.candidate_stop_atr_multiple,
            target_atr_multiple=self.settings.candidate_target_atr_multiple,
            htf_warmup_bars=self.settings.mtf_htf_warmup_bars,
        )
        variants: list[tuple[str, Any, Any, str]] = []
        if scope in {"control", "both"}:
            variants.append(("CONTROL", control_strategy, None, "fixed_usd"))
        if scope in {"candidate", "both"}:
            variants.append(("CANDIDATE", candidate_strategy, evaluate_mtf_tpr, "atr"))

        formatted_sections: list[str] = []
        artifact_names: list[str] = []
        for variant, strategy_cfg, evaluator, target_mode in variants:
            if job_id:
                self.store.update_backtest_job_progress(job_id, {
                    "stage": f"simulating_{variant.lower()}", "processed": len(loaded),
                    "total": len(loaded), "valid_symbols": len(loaded), "window": window,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
            config = RsiEmaBacktestConfig(
                starting_equity=self.settings.default_starting_balance,
                initial_margin=self.settings.initial_margin_usdt,
                max_leverage=self.settings.max_leverage,
                net_take_profit_usd=self.settings.net_take_profit_usdt,
                requested_stop_loss_usd=self.settings.requested_stop_loss_usdt,
                taker_fee_rate=self.settings.exchange_taker_fee_rate,
                slippage_bps_per_side=self.settings.backtest_slippage_bps_per_side,
                margin_mode=self.settings.backtest_margin_mode,
                max_holding_minutes=self.settings.backtest_max_holding_minutes,
                strategy=control_strategy if variant == "CONTROL" else self._rsi_ema_config(),
                target_mode=target_mode,
                max_positions=self.settings.max_concurrent_positions,
                risk_per_trade_fraction=self.settings.risk_per_trade_fraction,
                min_net_reward_risk=self.settings.candidate_min_net_rr,
                candidate_stop_atr_multiple=self.settings.candidate_stop_atr_multiple,
                candidate_target_atr_multiple=self.settings.candidate_target_atr_multiple,
            )
            signal_fn = None if variant == "CONTROL" else evaluator
            # First replay discovers which symbols actually open portfolio positions.
            # Support data for every raw signal would reintroduce the 7D memory spike.
            support_funding: dict[str, list[dict[str, Any]]] = {}
            funding_complete_by_symbol: dict[str, bool] = {}
            support_fair: dict[str, list[Candle]] = {}
            support_funding_price: dict[str, list[Candle]] = {}
            intrabar_candles: dict[str, list[Candle]] = {}
            report = await asyncio.to_thread(
                run_backtest_rsi_ema, loaded, window_days=longest, config=config,
                contract_specs=specs, funding_events=None,
                liquidation_candles_by_symbol=None,
                funding_price_candles_by_symbol=None, now_epoch=now,
                close_grace_seconds=self.settings.candle_close_grace_seconds,
                signal_evaluator=signal_fn,
            )
            # Only retain 1M execution paths for symbols whose provisional 3M replay
            # actually found a same-candle TP/SL ambiguity. The archive is worker-local
            # and persistent, so this does not require refetching public market history.
            ambiguous_symbols = sorted({
                str(row.get("symbol")) for row in report.get("trade_ledger", [])
                if row.get("exit_reason") == "STOP_LOSS_AMBIGUOUS_3M" and row.get("symbol")
            })
            for symbol in ambiguous_symbols:
                intrabar_candles[symbol] = await asyncio.to_thread(self.archive.load, symbol, history_start, now)
            if intrabar_candles:
                report = await asyncio.to_thread(
                    run_backtest_rsi_ema, loaded, window_days=longest, config=config,
                    contract_specs=specs, funding_events=None,
                    liquidation_candles_by_symbol=None,
                    funding_price_candles_by_symbol=None, now_epoch=now,
                    close_grace_seconds=self.settings.candle_close_grace_seconds,
                    signal_evaluator=signal_fn, intrabar_candles_by_symbol=intrabar_candles,
                )
            needed_symbols = sorted({str(row.get("symbol")) for row in report.get("trade_ledger", []) if row.get("symbol")})
            for support_symbol in needed_symbols:
                try:
                    funding_rows, funding_complete = await self.mexc.funding_history_window_with_coverage(
                        support_symbol, start=cutoff, end=now, max_pages=30,
                    )
                    support_funding[support_symbol] = list(funding_rows)
                    funding_complete_by_symbol[support_symbol] = bool(funding_complete)
                    if not funding_complete:
                        failures.append(f"{support_symbol}:FUNDING_HISTORY_PARTIAL")
                    fair_rows = await self.mexc.get_fair_price_klines_window(
                        support_symbol, "1M", start=history_start, end=now,
                        now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds,
                        max_bars_per_request=1800,
                    )
                    if fair_rows:
                        # Keep one 3M Fair Price series for both liquidation checks and
                        # funding settlement marks. Retaining 1M Fair Price candles for
                        # every symbol that traded multiplies 7D peak memory without
                        # adding useful resolution beyond the simulator's 3M event loop.
                        fair_3m = aggregate_3m_from_1m(fair_rows)
                        support_funding_price[support_symbol] = fair_3m
                        support_fair[support_symbol] = fair_3m
                        del fair_rows
                except Exception as exc:
                    failures.append(f"{support_symbol}:SUPPORT_{type(exc).__name__}")
                    logger.warning("BACKTEST SUPPORT DATA FAILED | symbol=%s error=%s", support_symbol, type(exc).__name__)
            # Re-run with fair-price liquidation and funding inputs for actual entered symbols.
            if needed_symbols:
                report = await asyncio.to_thread(
                    run_backtest_rsi_ema, loaded, window_days=longest, config=config,
                    contract_specs=specs, funding_events=support_funding,
                    liquidation_candles_by_symbol=support_fair,
                    funding_price_candles_by_symbol=support_funding_price,
                    now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds,
                    signal_evaluator=signal_fn, intrabar_candles_by_symbol=intrabar_candles,
                )
            if int(report.get("window_days", -1)) != longest:
                raise RuntimeError(f"Backtest horizon mismatch: requested {window}, engine returned {report.get('window_days')}D")
            report["window"] = window
            report["requested_window"] = window
            report["strategy_variant"] = variant
            report["strategy_version"] = self.settings.strategy_version if variant == "CONTROL" else "MTF-TPR-3M-15M-v1"
            report["strategy_config"] = asdict(strategy_cfg)
            report["assumptions"]["strategy_name"] = (
                "RSI_EMA_CONTROL_3M" if variant == "CONTROL" else "MTF_TREND_PULLBACK_RECLAIM_3M_15M"
            )
            report["assumptions"]["signal_engine_config"] = asdict(strategy_cfg)
            report["requested_start_epoch"] = cutoff
            report["requested_end_epoch"] = now
            report["requested_start_utc"] = datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            report["requested_end_utc"] = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            report["coins_tested"] = len(universe)
            report["coins_with_data"] = len(loaded)
            report["loaded_3m_candles_including_warmup"] = sum(len(v) for v in loaded.values())
            report["expected_universe_symbols"] = len(universe)
            report["valid_universe_symbols"] = len(loaded)
            report["coverage_by_symbol_pct"] = coverage_by_symbol
            expected_per_symbol = int(report.get("data_quality", {}).get("expected_completed_3m_bars_per_symbol", expected_3m))
            observed_window = sum(int(v) for v in report.get("data_quality", {}).get("bar_counts", {}).values())
            expected_window = expected_per_symbol * len(loaded)
            report["observed_window_3m_bars"] = observed_window
            report["expected_window_3m_bars"] = expected_window
            report["window_candle_coverage_pct"] = (observed_window / expected_window * 100.0) if expected_window else 0.0
            report["universe_symbol_coverage_pct"] = (len(loaded) / len(universe) * 100.0) if universe else 0.0
            report["minimum_universe_symbol_coverage_pct"] = self.settings.backtest_min_universe_coverage_pct
            report["universe_coverage_gate_passed"] = (
                True if args.get("symbol") else
                report["universe_symbol_coverage_pct"] >= self.settings.backtest_min_universe_coverage_pct
            )
            first_times = list(report.get("data_quality", {}).get("first_completed_bar_by_symbol", {}).values())
            last_times = list(report.get("data_quality", {}).get("last_completed_bar_by_symbol", {}).values())
            common_start = max(first_times) if first_times else None
            common_end = min(last_times) if last_times else None
            report["actual_data_start_utc"] = (
                datetime.fromtimestamp(common_start, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                if common_start is not None else "n/a"
            )
            report["actual_data_end_utc"] = (
                datetime.fromtimestamp(common_end, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                if common_end is not None else "n/a"
            )
            report["data_errors"] = list(failures)
            report["backtest_data_source"] = "MEXC 1M candles paginated from public Futures API and cached in worker-local persistent gzip archive; 3M derived only from complete UTC-aligned 1M triples"
            report["data_quality"]["support_data_requested_symbols"] = needed_symbols
            report["data_quality"]["funding_partial_coverage_symbols"] = sorted(
                s for s, complete in funding_complete_by_symbol.items() if not complete
            )
            report["data_quality"]["intrabar_1m_symbols"] = sorted(intrabar_candles)
            report["data_quality"]["universe_selection"] = "current 24h turnover top-N applied to historical window; point-in-time universe history unavailable"
            report["data_quality"]["survivorship_bias_warning"] = True
            report.setdefault("warnings", []).append(
                "Current top-turnover universe is applied to the historical period; point-in-time universe/survivorship bias remains."
            )
            report["data_quality"]["common_coverage_start_epoch"] = common_start
            report["data_quality"]["common_coverage_end_epoch"] = common_end
            report["data_quality"]["coverage_min_symbol_pct"] = min(coverage_by_symbol.values()) if coverage_by_symbol else 0.0
            report["data_quality"]["coverage_max_symbol_pct"] = max(coverage_by_symbol.values()) if coverage_by_symbol else 0.0
            report["data_quality"]["fair_price_missing_symbols"] = sorted(set(needed_symbols) - set(support_fair))
            report["data_quality"]["candidate_risk_budget_fraction"] = (
                self.settings.risk_per_trade_fraction if variant == "CANDIDATE" else None
            )
            report["data_quality"]["initial_margin_is_maximum_cap_not_risk_amount"] = variant == "CANDIDATE"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            artifact_paths = write_backtest_artifacts(
                report, self.settings.reports_dir, prefix=f"backtest_3m_{variant.lower()}_{window.lower()}_{stamp}",
            )
            run_id = self.store.create_research_run(f"{variant.lower()}_3m_{self.settings.backtest_margin_mode}", {
                "symbols": sorted(loaded), "window": window, "strategy_version": report["strategy_version"],
                "margin_mode": config.margin_mode, "starting_equity": config.starting_equity,
                "initial_margin_cap": config.initial_margin, "risk_fraction": config.risk_per_trade_fraction,
                "target_mode": target_mode, "universe": "top_100_by_24h_turnover" if not args.get("symbol") else "single_symbol",
            })
            report["artifact_paths"] = artifact_paths
            self.store.finish_research_run(run_id, report)
            formatted_sections.append(f"══ {variant} STRATEGY ══\\n" + format_backtest_whatsapp(
                report, elapsed_seconds=time.perf_counter() - started,
            ))
            artifact_names.extend(Path(x).name for x in artifact_paths.values())
            if job_id:
                self.store.update_backtest_job_progress(job_id, {
                    "stage": f"completed_{variant.lower()}", "processed": len(loaded),
                    "total": len(loaded), "valid_symbols": len(loaded), "signals": report.get("signals", 0),
                    "entered": report.get("trades_opened", 0), "resolved": report.get("resolved", 0),
                    "window": window, "updated_at": datetime.now(timezone.utc).isoformat(),
                })
            logger.info(
                "BACKTEST VARIANT COMPLETE | window=%s variant=%s universe=%d data=%d signals=%d entered=%d resolved=%d pnl=%.4f elapsed_s=%.1f",
                window, variant, len(universe), len(loaded), report.get("signals", 0),
                report.get("trades_opened", 0), report.get("resolved", 0), report.get("net_profit", 0.0),
                time.perf_counter() - started,
            )

        all_errors = f"\\nData quality exclusions/errors ({len(failures)}): " + ", ".join(failures[:20]) if failures else ""
        footer = (
            f"\\n\\nUniverse: top-{len(universe)} by 24H USDT turnover · valid data: {len(loaded)}/{len(universe)}"
            f" · 1M archive: {self.settings.market_data_dir} · Live orders OFF"
        )
        if artifact_names:
            footer += "\\nAudit artifacts: " + ", ".join(artifact_names[:6])
        return CommandResult("\\n\\n".join(formatted_sections) + all_errors + footer,
                             event_type="BACKTEST_3M_RESEARCH_COMPLETED")

    async def _run_backtest(self, args: dict[str, Any]) -> CommandResult:
        """Run the selected completed-bar strategy and return one concise WhatsApp reply."""
        command_started = time.perf_counter()
        symbols = [args["symbol"]] if args.get("symbol") else list(self.settings.default_symbols)
        windows = list(args.get("windows") or ["7D"])
        days_by_window = {"1D": 1, "3D": 3, "7D": 7, "30D": 30, "60D": 60,
                          "90D": 90, "180D": 180, "365D": 365}
        unknown_windows = [window for window in windows if window not in days_by_window]
        if unknown_windows:
            raise CommandError(f"Unsupported backtest window: {unknown_windows[0]}")

        try:
            now_epoch = await self.mexc.server_time()
            clock_warning: list[str] = []
        except Exception as exc:
            now_epoch = int(time.time())
            clock_warning = [f"Exchange time unavailable ({type(exc).__name__}); local UTC clock bounded the test window."]

        longest = max(days_by_window[window] for window in windows)
        loaded: dict[str, SymbolData] = {}
        data_load_errors: dict[str, str] = {}
        load_started = time.perf_counter()

        async def load_one(symbol: str):
            symbol_started = time.perf_counter()
            logger.info("BACKTEST SYMBOL LOAD START | symbol=%s window_days=%d", symbol, longest)
            data = await self.market_data.fetch_symbol(
                symbol, window_days=longest, daily_warmup_days=400,
                extra_intraday_warmup_days=45, now_epoch=now_epoch,
            )
            logger.info(
                "BACKTEST SYMBOL LOAD COMPLETE | symbol=%s bars=%s contract_metadata=%s elapsed_ms=%.0f",
                symbol,
                {tf: len(data.timeframes.get(tf, [])) for tf in ("1D", "12H", "4H", "1H")},
                bool(data.contract),
                (time.perf_counter() - symbol_started) * 1000,
            )
            return symbol, data

        load_results = await asyncio.gather(*(load_one(symbol) for symbol in symbols), return_exceptions=True)
        for symbol, item in zip(symbols, load_results):
            if isinstance(item, Exception):
                data_load_errors[symbol] = f"{type(item).__name__}: {str(item)[:160]}"
                self.store.append_event("BACKTEST_DATA_LOAD_FAILED", {
                    "symbol": symbol, "error_type": type(item).__name__, "window_days": longest,
                }, symbol=symbol, idempotency_key=f"backtest-load-failed:{symbol}:{now_epoch // 60}")
                logger.warning("BACKTEST SYMBOL LOAD FAILED | symbol=%s error_type=%s detail=%s",
                               symbol, type(item).__name__, str(item)[:180])
            else:
                loaded[symbol] = item[1]

        logger.info(
            "BACKTEST DATA LOAD COMPLETE | requested=%d loaded=%d failed=%d bars=%s elapsed_ms=%.0f",
            len(symbols), len(loaded), len(data_load_errors),
            {symbol: {tf: len(data.timeframes.get(tf, [])) for tf in ("1D", "12H", "4H", "1H")}
             for symbol, data in loaded.items()},
            (time.perf_counter() - load_started) * 1000,
        )

        series_map = {symbol: data.timeframes for symbol, data in loaded.items()}
        if not series_map:
            series_map = {symbol: {} for symbol in symbols}
        specs = {symbol: data.contract for symbol, data in loaded.items() if data.contract}
        mode = args.get("mode", "exchange_realistic")
        scenario = str(args.get("tp_scenario", "CONTROL"))
        reward_risk = float(args.get("reward_risk_multiple", 2.0))
        cfg = BacktestConfig(
            mode=mode, starting_balance=self.settings.default_starting_balance,
            tp_pct=self.settings.fixed_tp_pct, sl_pct=self.settings.fixed_sl_pct,
            fixed_win_payout=self.settings.fixed_win_payout, fixed_loss_payout=self.settings.fixed_loss_payout,
            risk_fraction=self.settings.risk_per_trade_fraction,
            taker_fee_rate=self.settings.exchange_taker_fee_rate,
            slippage_bps_per_side=self.settings.backtest_slippage_bps_per_side,
            reward_risk_multiple=reward_risk,
        )

        data_warnings = list(clock_warning)
        data_warnings.extend(
            f"{symbol}: market-data load failed ({error}); symbol not simulated."
            for symbol, error in data_load_errors.items()
        )
        for symbol, data in loaded.items():
            data_warnings.extend(f"{symbol}: {warning}" for warning in data.warnings)

        # First evaluate the strategy with no funding payload. Funding only changes
        # position P&L; it cannot alter a run that never opens a position. This avoids
        # paginating historical settlements for the common no-trade case.
        preflight_results: list[dict[str, Any]] = []
        if mode == "exchange_realistic":
            for window in windows:
                preflight = await asyncio.to_thread(
                    run_backtest, series_map, window=window, config=cfg,
                    contract_specs=specs, funding_events=None, now_epoch=now_epoch,
                )
                preflight_results.append(preflight)
            funding_needed = any(
                result.get("completed_trades", 0) or result.get("open_trades_at_end", 0)
                for result in preflight_results
            )
        else:
            funding_needed = False

        funding: dict[str, list[dict[str, Any]]] = {}
        funding_missing_symbols: list[str] = []
        funding_started = time.perf_counter()
        if mode == "exchange_realistic" and loaded and funding_needed:
            # Entries are only opened inside the test window and simulation ends at
            # `now_epoch`; candle warm-up history is not required for funding accrual.
            funding_start = now_epoch - longest * 86400

            async def load_funding(symbol: str):
                return symbol, await self.mexc.funding_history_window(
                    symbol, start=funding_start, end=now_epoch, max_pages=30,
                )

            funding_results = await asyncio.gather(
                *(load_funding(symbol) for symbol in loaded), return_exceptions=True,
            )
            for symbol, item in zip(loaded, funding_results):
                if isinstance(item, Exception):
                    funding[symbol] = []
                    funding_missing_symbols.append(symbol)
                    logger.warning("BACKTEST FUNDING LOAD FAILED | symbol=%s error_type=%s detail=%s",
                                   symbol, type(item).__name__, str(item)[:160])
                else:
                    funding[symbol] = item[1]
                    if not item[1]:
                        funding_missing_symbols.append(symbol)
            data_warnings.extend(
                f"{symbol}: funding history unavailable; modeled P&L excludes unknown funding."
                for symbol in funding_missing_symbols
            )
            logger.info(
                "BACKTEST FUNDING LOAD COMPLETE | symbols=%d missing=%d events=%d elapsed_ms=%.0f",
                len(funding), len(funding_missing_symbols), sum(len(rows) for rows in funding.values()),
                (time.perf_counter() - funding_started) * 1000,
            )
        elif mode == "exchange_realistic":
            logger.info(
                "BACKTEST FUNDING SKIPPED | reason=no_simulated_positions_or_trades elapsed_ms=%.0f",
                (time.perf_counter() - funding_started) * 1000,
            )

        results: list[dict[str, Any]] = []
        simulation_started = time.perf_counter()
        for window_index, window in enumerate(windows):
            window_started = time.perf_counter()
            if mode == "exchange_realistic" and not funding_needed:
                # No position was ever opened in this exact run, so its preflight
                # result is final and funding settlements cannot affect its P&L.
                result = preflight_results[window_index]
            else:
                result = await asyncio.to_thread(
                    run_backtest, series_map, window=window, config=cfg,
                    contract_specs=specs,
                    funding_events=funding if mode == "exchange_realistic" else None,
                    now_epoch=now_epoch,
                )
            result["warnings"] = list(dict.fromkeys(result["warnings"] + data_warnings))
            result["data_load_errors"] = dict(data_load_errors)
            result["strategy"] = "S1-RFPC: 1D regime → 12H structure → 4H setup → 1H trigger"
            result["signal_timeframes"] = ["1D", "12H", "4H", "1H"]
            result["tp_scenario"] = scenario
            result["reward_risk_multiple"] = reward_risk
            result["data_quality"] = {
                "requested_symbols": symbols,
                "loaded_symbols": sorted(loaded),
                "bar_counts": {
                    symbol: {tf: len(data.timeframes.get(tf, [])) for tf in ("1D", "12H", "4H", "1H")}
                    for symbol, data in loaded.items()
                },
                "contract_metadata_available": sorted(specs),
                "completed_candles_only": True,
            }

            # If there were no entries/open positions, cost multipliers cannot change
            # the result. Avoid rerunning the entire simulation three times for a zero-trade test.
            sensitivity: list[dict[str, Any]] = []
            has_activity = bool(result.get("completed_trades") or result.get("open_trades_at_end"))
            if mode == "exchange_realistic" and has_activity:
                multipliers = sorted(set((1.0, *self.settings.cost_stress_multipliers)))
                for multiplier in multipliers:
                    stress_result = result if multiplier == 1.0 else await asyncio.to_thread(
                        run_backtest, series_map, window=window,
                        config=replace(cfg, cost_multiplier=multiplier),
                        contract_specs=specs, funding_events=funding, now_epoch=now_epoch,
                    )
                    sensitivity.append({
                        "cost_multiplier": multiplier,
                        "completed_trades": stress_result["completed_trades"],
                        "wins": stress_result["wins"], "losses": stress_result["losses"],
                        "win_rate_pct": stress_result["win_rate_pct"],
                        "total_pnl": stress_result["total_pnl"],
                        "ending_balance": stress_result["ending_balance"],
                        "expectancy_per_trade": stress_result["expectancy_per_trade"],
                        "expectancy_r_per_trade": stress_result["expectancy_r_per_trade"],
                        "profit_factor": stress_result["profit_factor"],
                        "max_drawdown_pct": stress_result["max_drawdown_pct"],
                        "fees": stress_result["fees"], "funding_pnl": stress_result["funding_pnl"],
                        "slippage_cost": stress_result["slippage_cost"],
                    })
            result["cost_sensitivity"] = sensitivity

            run_id = self.store.create_research_run(mode, {
                "symbols": symbols, "window": window, "strategy_version": self.settings.strategy_version,
                "tp_scenario": scenario, "reward_risk_multiple": reward_risk,
                "config": {
                    "mode": cfg.mode, "tp_pct": cfg.tp_pct, "sl_pct": cfg.sl_pct,
                    "risk_fraction": cfg.risk_fraction, "taker_fee_rate": cfg.taker_fee_rate,
                    "slippage_bps_per_side": cfg.slippage_bps_per_side,
                    "cost_stress_multipliers": list(self.settings.cost_stress_multipliers),
                },
            })
            self.store.finish_research_run(run_id, result)
            results.append(result)
            logger.info(
                "BACKTEST WINDOW COMPLETE | window=%s status=%s symbols=%d/%d bars=%d evaluations=%d candidates=%d entry_attempts=%d trades=%d rejects=%s elapsed_ms=%.0f",
                window, result.get("status"), result.get("symbols_eligible", 0), len(symbols),
                result.get("valid_candles", 0), result.get("strategy_evaluations", 0),
                result.get("candidate_signals", 0), result.get("entry_attempts", 0),
                result.get("completed_trades", 0), result.get("rejection_reasons", {}),
                (time.perf_counter() - window_started) * 1000,
            )

        lines: list[str] = []
        for result in results:
            window = result["window"]
            if result.get("status") == "NOT_RUN_NO_DATA":
                lines.extend([f"🧪 BACKTEST {window} — NOT RUN", "No completed candle data was retrieved."])
                continue
            if result.get("status") == "NOT_RUN_INSUFFICIENT_HISTORY":
                lines.extend([f"🧪 BACKTEST {window} — NOT RUN", "Not enough completed history for the 1D EMA200 warm-up."])
                continue

            lines.append(f"🧪 BACKTEST {window} · {scenario}")
            lines.append(f"Symbols {result['symbols_eligible']}/{result['symbols_tested']} · 1H bars {result['valid_candles']}")
            lines.append(
                f"Evaluations {result.get('strategy_evaluations', 0)} · Candidates {result.get('candidate_signals', 0)} · Entries {result.get('entry_attempts', 0)}"
            )
            if result.get("completed_trades", 0) == 0 and result.get("open_trades_at_end", 0) == 0:
                lines.append("No qualifying trades in this period.")
                blockers = sorted(result.get("rejection_reasons", {}).items(), key=lambda item: (-item[1], item[0]))[:3]
                if blockers:
                    lines.append("Main filters: " + " · ".join(f"{name.replace('_', ' ').lower()} ×{count}" for name, count in blockers))
            else:
                win_rate = result.get("win_rate_pct")
                win_rate_text = "n/a" if win_rate is None else f"{win_rate:.1f}%"
                lines.append(
                    f"Trades {result['completed_trades']} · W/L {result['wins']}/{result['losses']} · Win {win_rate_text}"
                )
                lines.append(
                    f"Net P&L ${result['total_pnl']:+.2f} · Equity ${result['ending_balance']:.2f} · DD {result['max_drawdown_pct']:.2f}%"
                )
            if result.get("cost_sensitivity"):
                stress = " · ".join(
                    f"{item['cost_multiplier']:.1f}× ${item['total_pnl']:+.2f}"
                    for item in result["cost_sensitivity"]
                )
                lines.append(f"Cost stress: {stress}")
            if result.get("warnings") and (data_load_errors or funding_missing_symbols or clock_warning):
                lines.append("Data note: " + " · ".join(result["warnings"][:2]))

        lines.append("Live orders: OFF")
        total_ms = (time.perf_counter() - command_started) * 1000
        logger.info(
            "BACKTEST COMMAND COMPLETE | windows=%s requested_symbols=%d loaded_symbols=%d elapsed_ms=%.0f",
            windows, len(symbols), len(loaded), total_ms,
        )
        return CommandResult("\n".join(lines), event_type="BACKTEST_COMPLETED")

    def _apply_selected_price_series(self, candles: list[Candle]) -> list[Candle]:
        """Apply one consistent OHLC series; never silently mix real and adjusted prices."""
        if self.settings.mexc_price_series != "real":
            return list(candles)
        if any(any(v is None for v in (c.real_open, c.real_high, c.real_low, c.real_close)) for c in candles):
            raise MexcError(
                "Configured real OHLC series is incomplete; no substitute price series was used. "
                "Review the MEXC data probe before changing MEXC_PRICE_SERIES."
            )
        return [replace(c, open=float(c.real_open), high=float(c.real_high),
                        low=float(c.real_low), close=float(c.real_close)) for c in candles]


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _first_number(mapping: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if number > 0:
            return number
    return None


def _validate_backtest_contract_spec(
    spec: dict[str, Any], *, margin_mode: str = "cross",
) -> tuple[bool, str]:
    """Require verified, compatible USDT-perpetual metadata before sizing a position."""
    state = spec.get("state")
    try:
        if state is None or int(state) != 0:
            return False, "CONTRACT_NOT_VERIFIED_ACTIVE"
    except (TypeError, ValueError, OverflowError):
        return False, "CONTRACT_STATE_INVALID"

    future_type = spec.get("futureType")
    if future_type is None:
        return False, "CONTRACT_METADATA_MISSING:futureType"
    try:
        if int(future_type) != 1:
            return False, "CONTRACT_NOT_USDT_PERPETUAL"
    except (TypeError, ValueError, OverflowError):
        return False, "CONTRACT_FUTURE_TYPE_INVALID"

    quote_raw = spec.get("quoteCoin")
    settle_raw = spec.get("settleCoin")
    if quote_raw is None or settle_raw is None:
        return False, "CONTRACT_METADATA_MISSING:quoteCoin_or_settleCoin"
    if str(quote_raw).upper() != "USDT" or str(settle_raw).upper() != "USDT":
        return False, "CONTRACT_NOT_USDT_SETTLED"

    position_open_type = spec.get("positionOpenType")
    if position_open_type is None:
        return False, "CONTRACT_METADATA_MISSING:positionOpenType"
    try:
        open_type = int(position_open_type)
    except (TypeError, ValueError, OverflowError):
        return False, "CONTRACT_POSITION_MODE_INVALID"
    # GET Contract Info defines 1=isolated-only, 2=cross-only, 3=both.
    if margin_mode == "cross" and open_type not in {2, 3}:
        return False, "CONTRACT_CROSS_MARGIN_UNSUPPORTED"
    if margin_mode == "isolated" and open_type not in {1, 3}:
        return False, "CONTRACT_ISOLATED_MARGIN_UNSUPPORTED"

    required = {
        "contractSize": ("contractSize", "contract_size"),
        "volUnit": ("volUnit", "volumeUnit", "quantityStep"),
        "minVol": ("minVol", "minVolume", "minOrderQty"),
        "maxVol": ("maxVol", "maxVolume", "maxOrderQty"),
        "priceUnit": ("priceUnit", "price_unit"),
        "maintenanceMarginRate": ("maintenanceMarginRate", "maintenance_margin_rate", "mmr"),
    }
    for label, keys in required.items():
        value = _first_number(spec, *keys)
        if value is None or value <= 0:
            return False, f"CONTRACT_METADATA_INVALID:{label}"
    leverage = _first_number(spec, "countryConfigContractMaxLeverage", "maxLeverage", "max_leverage")
    if leverage is None or leverage < 1:
        return False, "CONTRACT_METADATA_INVALID:MAX_LEVERAGE"
    return True, "OK"
