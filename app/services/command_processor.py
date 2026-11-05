"""Command dispatch for the official WhatsApp adapter; all processing is deterministic."""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.alerts import AlertService
from app.analysis import analyze_series, format_analysis
from app.backtest import BacktestConfig, run_backtest
from app.charting import render_candle_chart
from app.commands import Command, CommandError, HELP_TEXT, parse_command
from app.config import Settings
from app.market.archive import CandleArchive
from app.market.candles import Candle, INTERVAL_SECONDS, aggregate_12h_from_4h
from app.mexc.client import MexcClient, MexcError
from app.services.market_data import MarketDataService, SymbolData
from app.services.market_quality import (
    candle_window_coverage, funding_events_for_window, validate_usdt_perpetual_contract,
)
from app.services.universe import top_usdt_perpetual_symbols
from app.storage import Store, canonical_json
from app.strategy.rfpc import STRATEGY_VERSION
from app.symbols import normalize_symbol
from app.whatsapp import WhatsAppCloud, sender_key

logger = logging.getLogger(__name__)


def _final_funding_coverage(
    preflight_coverage: dict[str, bool],
    trades: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
) -> tuple[dict[str, bool], list[str], str]:
    """Reconcile funding coverage against symbols in the actual final portfolio run.

    The preflight pass can select a different portfolio from the funded pass. Any symbol
    introduced only by the final run must be marked incomplete, never silently treated
    as having zero funding.
    """
    coverage = {str(symbol): bool(complete) for symbol, complete in preflight_coverage.items()}
    traded_symbols = sorted({
        str(row.get("symbol")) for row in trades if row.get("symbol")
    } | {
        str(row.get("symbol")) for row in open_positions if row.get("symbol")
    })
    for symbol in traded_symbols:
        coverage.setdefault(symbol, False)
    missing = [symbol for symbol in traded_symbols if not coverage.get(symbol, False)]
    status = "NOT_REQUIRED" if not traded_symbols else ("INCOMPLETE" if missing else "COMPLETE")
    return coverage, missing, status


def _process_rss_mb() -> float | None:
    """Best-effort current resident-set measurement on Linux/Render."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                return round(float(parts[1]) / 1024.0, 2)
    except (OSError, ValueError, IndexError):
        pass
    return None


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
                           enforce_sender_allowlist: bool = True) -> CommandResult:
        # Authorize before parsing to avoid exposing command grammar to unauthorized senders.
        allowed = {sender_key(x) for x in self.settings.whatsapp_allowed_senders}
        if enforce_sender_allowlist and (not allowed or sender_key(sender) not in allowed):
            raise CommandError("Sender is not authorized")
        # A signed, persisted webhook delivery remains valid after background queueing.
        # Meta can deliver retries and our own workers can be briefly delayed; a five-minute
        # age check here incorrectly rejected commands that had already been accepted.
        # Duplicate execution is handled by the inbound message ID/idempotency key instead.
        command = parse_command(text)
        if command.name == "BACKTEST":
            command.args["_recipient"] = sender_key(sender) if enforce_sender_allowlist else ""
            command.args["_request_id"] = request_id or f"local-{time.time_ns()}"
        elif command.name in {"BACKTEST_STATUS", "BACKTEST_REPORT"}:
            command.args["_recipient"] = sender_key(sender) if enforce_sender_allowlist else ""
        if command.name == "BACKTEST":
            logger.info(
                "BACKTEST COMMAND PARSED | raw=%r requested_window=%s symbol=%s",
                text.strip(), command.args.get("window"), command.args.get("symbol", "TOP_UNIVERSE"),
            )
        result = await self.execute(command)
        self.store.append_event("WHATSAPP_COMMAND_PROCESSED" if enforce_sender_allowlist else "LOCAL_COMMAND_PROCESSED",
            {"sender": sender_key(sender) if enforce_sender_allowlist else "authenticated-web-dashboard",
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
                f"WhatsApp sender allowlist: {'CONFIGURED' if self.settings.whatsapp_allowed_senders else 'NOT CONFIGURED'}\n"
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
            return await self._scan_top_100_swing()
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
                    f"🧪 BACKTEST JOB QUEUED\nJob: {job_id}\nPeriod: {args.get('window')}\nStrategy: S1-RFPC (1D → 12H → 4H → 1H)\nPortfolio cap: {self.settings.backtest_max_positions} open research positions\nThe dedicated worker will process this job. Use BACKTEST REPORT {job_id} or BACKTEST STATUS. Live orders OFF.",
                    event_type="BACKTEST_JOB_QUEUED",
                )
            async with self._backtest_lock:
                return await self._run_backtest(args)
        if name == "BACKTEST_STATUS":
            recipient = str(args.get("_recipient") or "") or None
            jobs = self.store.recent_backtest_jobs(8, recipient=recipient)
            if not jobs:
                return CommandResult("No backtest jobs found for this sender.")
            lines = ["BACKTEST JOBS"]
            for job in jobs:
                progress = job.get("progress") or {}
                if progress:
                    pct = progress.get("percent")
                    pct_text = f" {pct:.0f}%" if isinstance(pct, (int, float)) else ""
                    cursor = f" {progress.get('processed', 0)}/{progress.get('total', 0)}{pct_text}"
                    last_symbol = progress.get("last_completed_symbol") or progress.get("current_symbol")
                    symbol_text = f" · {last_symbol}" if last_symbol else ""
                    detail = f" · {progress.get('stage', '')}{cursor}{symbol_text}"
                else:
                    detail = ""
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
                active = self.store.active_signals(
                    now_epoch=int(time.time()), limit=200, strategy_version=STRATEGY_VERSION,
                )
                if not active:
                    summary = self.store.get_state("top100_swing_scan_summary", "No completed S1-RFPC scan summary yet.")
                    last = self.store.get_state("top100_swing_scan_last_success", "never")
                    return CommandResult(
                        "ACTIVE S1-RFPC SWING SIGNALS\nNo unexpired, validated signals currently available.\n"
                        f"Last successful scan: {last}\nScanner: {summary}"
                    )
                lines = [f"ACTIVE S1-RFPC SWING SIGNALS — {len(active)}"]
                for row in active[:20]:
                    signal = row["payload"]
                    expiry = datetime.fromtimestamp(int(row["expires_at"]), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    delivery = "PUSH-ELIGIBLE" if signal.get("delivery_eligible") else "DELIVERY BLOCKED"
                    reasons = ",".join(signal.get("delivery_block_reasons") or [])
                    lines.append(
                        f"{row['symbol']} {row['direction']} @ {float(signal.get('entry_reference', signal.get('close', 0.0))):.8g} "
                        f"· gross RR {float(signal.get('planned_price_rr', 0.0)):.2f}R "
                        f"· net RR {float(signal.get('planned_net_rr', 0.0)):.2f}R · {delivery} · expires {expiry}"
                    )
                    if reasons:
                        lines.append(f"  Delivery checks: {reasons}")
                lines.append("Strategy: 1D → 12H → 4H → 1H completed candles. Research only; no exchange orders.")
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
                f"Paper simulator position cap: {self.settings.max_concurrent_positions}\n"
                f"Backtest research position cap: {self.settings.backtest_max_positions}\n"
                f"Backtest aggregate planned stop-risk cap: {self.settings.backtest_max_open_risk_fraction:.2%}\n"
                "Research settings do not enable live trading and are not a recommendation to trade."
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

    async def _run_backtest(self, args: dict[str, Any]) -> CommandResult:
        """Run the completed-bar 1D→12H→4H→1H strategy on a bounded symbol universe."""
        command_started = time.perf_counter()
        job_id = str(args.get("_job_id") or "")
        windows = list(args.get("windows") or [str(args.get("window") or "7D")])
        logger.info(
            "BACKTEST START | job_id=%s windows=%s symbol=%s strategy=S1-RFPC timeframes=1D,12H,4H,1H",
            job_id or "inline", windows, args.get("symbol") or "TOP_UNIVERSE",
        )
        if job_id:
            self.store.update_backtest_job_progress(job_id, {
                "stage": "preparing_market_data", "processed": 0, "total": 0,
                "percent": 0.0, "strategy_timeframes": ["1D", "12H", "4H", "1H"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        days_by_window = {"1D": 1, "3D": 3, "7D": 7, "30D": 30, "60D": 60,
                          "90D": 90, "180D": 180, "365D": 365}
        unknown_windows = [window for window in windows if window not in days_by_window]
        if unknown_windows:
            raise CommandError(f"Unsupported backtest window: {unknown_windows[0]}")

        logger.info("BACKTEST EXCHANGE TIME REQUEST START | job_id=%s", job_id or "inline")
        try:
            now_epoch = await self.mexc.server_time()
            clock_warning: list[str] = []
            logger.info("BACKTEST EXCHANGE TIME READY | job_id=%s source=exchange", job_id or "inline")
        except Exception as exc:
            now_epoch = int(time.time())
            clock_warning = [f"Exchange time unavailable ({type(exc).__name__}); local UTC clock bounded the test window."]

        universe_rows: list[dict[str, Any]] = []
        if args.get("symbol"):
            symbols = [str(args["symbol"])]
            universe_note = "single symbol"
        else:
            logger.info(
                "BACKTEST UNIVERSE LOAD START | job_id=%s top_n=%d",
                job_id or "inline", self.settings.mexc_universe_top_n,
            )
            universe_rows = await self._load_top_universe(self.settings.mexc_universe_top_n)
            symbols = [str(row["symbol"]) for row in universe_rows]
            universe_note = f"top {len(symbols)} USDT perpetuals by 24H turnover"
            logger.info(
                "BACKTEST UNIVERSE LOAD COMPLETE | job_id=%s symbols=%d",
                job_id or "inline", len(symbols),
            )
        if not symbols:
            raise CommandError("No eligible MEXC USDT perpetual symbols were returned for this backtest.")

        longest = max(days_by_window[window] for window in windows)
        loaded: dict[str, SymbolData] = {}
        data_load_errors: dict[str, str] = {}
        load_started = time.perf_counter()

        job_total = len(symbols)
        if job_id:
            self.store.update_backtest_job_progress(job_id, {
                "stage": "loading_historical_data", "processed": 0, "total": job_total,
                "percent": 0.0, "loaded": 0, "failed": 0,
                "strategy_timeframes": ["1D", "12H", "4H", "1H"],
            })
        logger.info(
            "BACKTEST SYMBOL SCAN START | job_id=%s universe=%s symbols=%d window_days=%d strategy=1D-12H-4H-1H",
            job_id or "inline", universe_note, job_total, longest,
        )
        semaphore = asyncio.Semaphore(min(5, max(1, len(symbols))))

        async def load_one(index: int, symbol: str):
            symbol_started = time.perf_counter()
            async with semaphore:
                logger.info("BACKTEST SYMBOL LOAD START | progress_index=%d/%d symbol=%s window_days=%d",
                            index, job_total, symbol, longest)
                try:
                    data = await self.market_data.fetch_symbol(
                        symbol, window_days=longest, daily_warmup_days=400,
                        extra_intraday_warmup_days=45, now_epoch=now_epoch,
                    )
                    logger.info(
                        "BACKTEST SYMBOL LOAD COMPLETE | progress_index=%d/%d symbol=%s bars=%s contract_metadata=%s elapsed_ms=%.0f",
                        index, job_total, symbol,
                        {tf: len(data.timeframes.get(tf, [])) for tf in ("1D", "12H", "4H", "1H")},
                        bool(data.contract), (time.perf_counter() - symbol_started) * 1000,
                    )
                    return symbol, data, None
                except Exception as exc:
                    logger.warning("BACKTEST SYMBOL LOAD FAILED | progress_index=%d/%d symbol=%s error_type=%s detail=%s",
                                   index, job_total, symbol, type(exc).__name__, str(exc)[:180])
                    return symbol, None, exc

        tasks = [asyncio.create_task(load_one(index, symbol)) for index, symbol in enumerate(symbols, 1)]
        processed = 0
        for completed_task in asyncio.as_completed(tasks):
            symbol, data, error = await completed_task
            processed += 1
            if error is not None:
                data_load_errors[symbol] = f"{type(error).__name__}: {str(error)[:160]}"
                self.store.append_event("BACKTEST_DATA_LOAD_FAILED", {
                    "job_id": job_id or None, "symbol": symbol,
                    "error_type": type(error).__name__, "window_days": longest,
                }, symbol=symbol, idempotency_key=f"backtest-load-failed:{job_id or now_epoch}:{symbol}")
            else:
                loaded[symbol] = data
            progress = {
                "stage": "loading_historical_data", "processed": processed, "total": job_total,
                "percent": round(processed / job_total * 100, 1),
                "loaded": len(loaded), "failed": len(data_load_errors),
                "last_completed_symbol": symbol,
                "process_rss_mb": _process_rss_mb(),
                "strategy_timeframes": ["1D", "12H", "4H", "1H"],
            }
            if job_id:
                self.store.update_backtest_job_progress(job_id, progress)
            logger.info(
                "BACKTEST SYMBOL SCAN | progress=%d/%d (%.1f%%) symbol=%s loaded=%d failed=%d rss_mb=%s",
                processed, job_total, processed / job_total * 100, symbol, len(loaded), len(data_load_errors),
                _process_rss_mb(),
            )

        logger.info(
            "BACKTEST DATA LOAD COMPLETE | requested=%d loaded=%d failed=%d rss_mb=%s bars=%s elapsed_ms=%.0f",
            len(symbols), len(loaded), len(data_load_errors), _process_rss_mb(),
            {symbol: {tf: len(data.timeframes.get(tf, [])) for tf in ("1D", "12H", "4H", "1H")}
             for symbol, data in loaded.items()},
            (time.perf_counter() - load_started) * 1000,
        )

        mode = "exchange_realistic"
        scenario = "S1-RFPC"
        reward_risk = float(args.get("reward_risk_multiple", 2.0))
        cfg = BacktestConfig(
            mode=mode, starting_balance=self.settings.default_starting_balance,
            tp_pct=self.settings.fixed_tp_pct, sl_pct=self.settings.fixed_sl_pct,
            fixed_win_payout=self.settings.fixed_win_payout, fixed_loss_payout=self.settings.fixed_loss_payout,
            risk_fraction=self.settings.risk_per_trade_fraction,
            taker_fee_rate=self.settings.exchange_taker_fee_rate,
            slippage_bps_per_side=self.settings.backtest_slippage_bps_per_side,
            reward_risk_multiple=reward_risk,
            max_concurrent_positions=self.settings.backtest_max_positions,
            max_open_risk_fraction=self.settings.backtest_max_open_risk_fraction,
            margin_mode=self.settings.backtest_margin_mode,
            initial_margin_per_position=self.settings.initial_margin_usdt,
            max_leverage=self.settings.max_leverage,
            max_total_notional_multiple=self.settings.backtest_max_total_notional_multiple,
            liquidation_fee_rate_fallback=self.settings.backtest_liquidation_fee_rate,
            max_holding_hours=max(1, self.settings.backtest_max_holding_minutes) / 60.0,
        )

        # Validate the instrument and exact requested-window candle grid before it can
        # contribute to exchange-realistic results. Warm-up history is still supplied
        # to the strategy, but coverage is measured only over the requested test window.
        data_warnings = list(clock_warning)
        data_warnings.extend(
            f"{symbol}: market-data load failed ({error}); symbol not simulated."
            for symbol, error in data_load_errors.items()
        )
        for symbol, data in loaded.items():
            data_warnings.extend(f"{symbol}: {warning}" for warning in data.warnings)

        contract_validation_errors: dict[str, str] = {}
        valid_contracts: dict[str, dict[str, Any]] = {}
        for symbol, data in loaded.items():
            ok, reason = _validate_backtest_contract_spec(data.contract or {})
            if ok:
                valid_contracts[symbol] = dict(data.contract or {})
            else:
                contract_validation_errors[symbol] = reason
                data_warnings.append(f"{symbol}: contract rejected by backtest validator ({reason}).")

        window_series_maps: dict[str, dict[str, dict[str, list[Candle]]]] = {}
        window_specs_maps: dict[str, dict[str, dict[str, Any]]] = {}
        coverage_by_window: dict[str, dict[str, dict[str, Any]]] = {}
        window_universe_coverage: dict[str, float] = {}
        for window in windows:
            window_start = now_epoch - days_by_window[window] * 86400
            eligible_series: dict[str, dict[str, list[Candle]]] = {}
            eligible_specs: dict[str, dict[str, Any]] = {}
            per_symbol_coverage: dict[str, dict[str, Any]] = {}
            excluded: dict[str, str] = dict(contract_validation_errors)
            for symbol, data in loaded.items():
                if symbol not in valid_contracts:
                    continue
                coverage = candle_window_coverage(
                    data.timeframes, start_time=window_start, end_time=now_epoch,
                )
                per_symbol_coverage[symbol] = coverage
                bad_timeframes = [
                    tf for tf, metric in coverage.items()
                    if not metric["continuous"]
                    or metric["coverage_pct"] < self.settings.backtest_min_coverage_pct
                ]
                if bad_timeframes:
                    summary = ",".join(
                        f"{tf}={coverage[tf]['coverage_pct']:.2f}%" for tf in bad_timeframes
                    )
                    excluded[symbol] = f"CANDLE_COVERAGE_OR_CONTINUITY_FAILED:{summary}"
                    continue
                eligible_series[symbol] = data.timeframes
                eligible_specs[symbol] = valid_contracts[symbol]
            window_series_maps[window] = eligible_series
            window_specs_maps[window] = eligible_specs
            coverage_by_window[window] = per_symbol_coverage
            coverage_pct = len(eligible_series) / max(1, len(symbols)) * 100.0
            window_universe_coverage[window] = coverage_pct
            if excluded:
                data_warnings.extend(
                    f"{window} {symbol}: {reason}." for symbol, reason in excluded.items()
                )

        if job_id:
            self.store.update_backtest_job_progress(job_id, {
                "stage": "simulating_portfolio", "processed": job_total, "total": job_total,
                "percent": 90.0, "loaded": len(loaded), "failed": len(data_load_errors),
                "configured_max_positions": cfg.max_concurrent_positions,
            })

        # First evaluate each eligible window with no funding payload. Funding only
        # changes P&L for positions that actually open. A window that fails either
        # configured coverage gate is explicitly NOT RUN, never silently simulated.
        preflight_results: list[dict[str, Any]] = []
        for window in windows:
            eligible = window_series_maps[window]
            universe_pct = window_universe_coverage[window]
            if (not eligible or
                    universe_pct < self.settings.backtest_min_universe_coverage_pct):
                preflight_results.append({
                    "status": "NOT_RUN_INSUFFICIENT_DATA_COVERAGE", "window": window,
                    "start_time": now_epoch - days_by_window[window] * 86400,
                    "end_time": now_epoch, "symbols_tested": len(symbols),
                    "symbols_eligible": len(eligible), "valid_candles": 0,
                    "completed_trades": 0, "open_trades_at_end": 0,
                    "warnings": [
                        f"Universe coverage {universe_pct:.2f}% is below required "
                        f"{self.settings.backtest_min_universe_coverage_pct:.2f}%; "
                        "this window was not simulated."
                    ],
                    "data_quality": {},
                })
                continue
            result = await asyncio.to_thread(
                run_backtest, eligible, window=window, config=cfg,
                contract_specs=window_specs_maps[window], funding_events=None, now_epoch=now_epoch,
            )
            preflight_results.append(result)

        funding_needed_by_window = {
            window: mode == "exchange_realistic" and bool(
                preflight_results[index].get("completed_trades", 0)
                or preflight_results[index].get("open_trades_at_end", 0)
            )
            for index, window in enumerate(windows)
        }
        funded_symbols_by_window: dict[str, list[str]] = {}
        funding_by_window: dict[str, dict[str, list[dict[str, Any]]]] = {}
        funding_coverage_by_window: dict[str, dict[str, bool]] = {}
        funding_missing_by_window: dict[str, list[str]] = {}
        funding_warnings_by_window: dict[str, list[str]] = {}
        funding_started = time.perf_counter()

        funding_history_by_symbol: dict[str, list[dict[str, Any]]] = {}
        funding_history_complete_by_symbol: dict[str, bool] = {}
        funded_symbols = sorted({
            str(trade.get("symbol")) for report in preflight_results
            for trade in report.get("trades", []) if trade.get("symbol")
        } | {
            str(position.get("symbol")) for report in preflight_results
            for position in report.get("open_positions", []) if position.get("symbol")
        })
        for index, window in enumerate(windows):
            report = preflight_results[index]
            symbols_for_window = sorted({
                str(trade.get("symbol")) for trade in report.get("trades", [])
                if trade.get("symbol")
            } | {
                str(position.get("symbol")) for position in report.get("open_positions", [])
                if position.get("symbol")
            })
            funded_symbols_by_window[window] = symbols_for_window
            funding_by_window[window] = {}
            funding_coverage_by_window[window] = {}
            funding_missing_by_window[window] = []
            funding_warnings_by_window[window] = []

        # Fetch once per symbol at the widest requested horizon. A wider history
        # can be sliced for all shorter reports, avoiding duplicate rate-limited
        # MEXC requests for commands such as BACKTEST 1D,7D,30D.
        all_funding_needed = any(funding_needed_by_window.values())
        if mode == "exchange_realistic" and all_funding_needed:
            funding_start = now_epoch - longest * 86400
            funding_symbols = [symbol for symbol in funded_symbols if symbol in loaded]

            async def load_funding(symbol: str):
                return symbol, await self.mexc.funding_history_window_with_coverage(
                    symbol, start=funding_start, end=now_epoch, max_pages=30,
                )

            funding_results = await asyncio.gather(
                *(load_funding(symbol) for symbol in funding_symbols), return_exceptions=True,
            )
            for symbol, item in zip(funding_symbols, funding_results):
                if isinstance(item, Exception):
                    funding_history_by_symbol[symbol] = []
                    funding_history_complete_by_symbol[symbol] = False
                    logger.warning(
                        "BACKTEST FUNDING LOAD FAILED | symbol=%s error_type=%s detail=%s",
                        symbol, type(item).__name__, str(item)[:160],
                    )
                else:
                    events, complete = item[1]
                    funding_history_by_symbol[symbol] = events
                    funding_history_complete_by_symbol[symbol] = bool(complete)

            for window in windows:
                if not funding_needed_by_window[window]:
                    continue
                requested_start = now_epoch - days_by_window[window] * 86400
                for symbol in funded_symbols_by_window[window]:
                    if symbol not in loaded or symbol not in funding_history_by_symbol:
                        events, complete = [], False
                    else:
                        events, complete = funding_events_for_window(
                            funding_history_by_symbol[symbol],
                            history_complete=funding_history_complete_by_symbol[symbol],
                            requested_start=requested_start, requested_end=now_epoch,
                        )
                    funding_by_window[window][symbol] = events
                    funding_coverage_by_window[window][symbol] = bool(complete)
                    if not complete:
                        funding_missing_by_window[window].append(symbol)
                        funding_warnings_by_window[window].append(
                            f"{symbol}: funding history for {window} is incomplete; P&L includes only observed settlements and is not fully funding-adjusted."
                        )
            logger.info(
                "BACKTEST FUNDING LOAD COMPLETE | symbols=%d incomplete_longest=%d events=%d elapsed_ms=%.0f",
                len(funding_history_by_symbol),
                sum(not value for value in funding_history_complete_by_symbol.values()),
                sum(len(rows) for rows in funding_history_by_symbol.values()),
                (time.perf_counter() - funding_started) * 1000,
            )
        elif mode == "exchange_realistic":
            logger.info(
                "BACKTEST FUNDING SKIPPED | reason=no_simulated_positions_or_trades elapsed_ms=%.0f",
                (time.perf_counter() - funding_started) * 1000,
            )

        results: list[dict[str, Any]] = []
        for window_index, window in enumerate(windows):
            window_started = time.perf_counter()
            if mode == "exchange_realistic" and not funding_needed_by_window[window]:
                # No position was ever opened in this exact run, so its preflight
                # result is final and funding settlements cannot affect its P&L.
                result = preflight_results[window_index]
            elif preflight_results[window_index].get("status") == "NOT_RUN_INSUFFICIENT_DATA_COVERAGE":
                result = dict(preflight_results[window_index])
            else:
                result = await asyncio.to_thread(
                    run_backtest, window_series_maps[window], window=window, config=cfg,
                    contract_specs=window_specs_maps[window],
                    funding_events=funding_by_window[window] if mode == "exchange_realistic" else None,
                    now_epoch=now_epoch,
                )
            final_funding_coverage, final_funding_missing, final_funding_status = (
                _final_funding_coverage(
                    funding_coverage_by_window[window],
                    list(result.get("trades", [])),
                    list(result.get("open_positions", [])),
                ) if mode == "exchange_realistic" else ({}, [], "NOT_REQUIRED")
            )
            final_funding_warnings = [
                f"{symbol}: final portfolio position has no complete funding history; P&L is not fully funding-adjusted."
                for symbol in final_funding_missing
            ]
            result["warnings"] = list(dict.fromkeys(
                result["warnings"] + data_warnings + funding_warnings_by_window[window] + final_funding_warnings
            ))
            result["data_load_errors"] = dict(data_load_errors)
            result["strategy"] = "S1-RFPC: 1D regime → 12H structure → 4H setup → 1H trigger"
            result["signal_timeframes"] = ["1D", "12H", "4H", "1H"]
            result["tp_scenario"] = scenario
            result["reward_risk_multiple"] = reward_risk
            result["data_quality"] = {
                "requested_symbols": symbols,
                "loaded_symbols": sorted(loaded),
                "eligible_symbols": sorted(window_series_maps[window]),
                "universe_coverage_pct": window_universe_coverage[window],
                "required_min_coverage_pct": self.settings.backtest_min_coverage_pct,
                "required_min_universe_coverage_pct": self.settings.backtest_min_universe_coverage_pct,
                "candle_coverage": coverage_by_window[window],
                "contract_validation_errors": contract_validation_errors,
                "funding_history_coverage": final_funding_coverage,
                "funding_accounting_complete": (
                    None if final_funding_status == "NOT_REQUIRED" else final_funding_status == "COMPLETE"
                ),
                "funding_accounting_status": final_funding_status,
                "funding_partial_coverage_symbols": final_funding_missing,
                "bar_counts": {
                    symbol: {tf: len(data.timeframes.get(tf, [])) for tf in ("1D", "12H", "4H", "1H")}
                    for symbol, data in loaded.items()
                },
                "contract_metadata_available": sorted(valid_contracts),
                "completed_candles_only": True,
                "universe_snapshot_type": "CURRENT_TOP_TURNOVER_NOT_POINT_IN_TIME" if universe_rows else "SINGLE_SYMBOL",
                "universe_snapshot_time_utc": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
            }
            funding_status = result["data_quality"]["funding_accounting_status"]
            if funding_status == "INCOMPLETE":
                result["validation_status"] = "NOT_VALIDATION_READY_FUNDING_INCOMPLETE"
            elif result.get("risk_tier_status_counts", {}).get("BASE_MMR_FALLBACK", 0):
                result["validation_status"] = "NOT_VALIDATION_READY_BASE_MMR_FALLBACK"
            elif result.get("unverified_liquidation_fee_positions", 0):
                result["validation_status"] = "NOT_VALIDATION_READY_LIQUIDATION_FEE_MISSING"
            else:
                result["validation_status"] = "RESEARCH_ONLY_MODELED_LIMITATIONS"
            result["fully_exchange_validated"] = False
            if result.get("status") == "INVALID_RECONCILIATION":
                result["validation_status"] = "INVALID_REPORT_RECONCILIATION"
            if job_id:
                self.store.update_backtest_job_progress(job_id, {
                    "stage": f"simulating_{window}", "processed": job_total, "total": job_total,
                    "percent": round(90 + 10 * (window_index + 1) / len(windows), 1),
                    "loaded": len(loaded), "failed": len(data_load_errors),
                    "configured_max_positions": cfg.max_concurrent_positions,
                })

            # If there were no entries/open positions, cost multipliers cannot change
            # the result. Avoid rerunning the entire simulation three times for a zero-trade test.
            sensitivity: list[dict[str, Any]] = []
            has_activity = bool(result.get("completed_trades") or result.get("open_trades_at_end"))
            if mode == "exchange_realistic" and has_activity:
                multipliers = sorted(set((1.0, *self.settings.cost_stress_multipliers)))
                for multiplier in multipliers:
                    stress_result = result if multiplier == 1.0 else await asyncio.to_thread(
                        run_backtest, window_series_maps[window], window=window,
                        config=replace(cfg, cost_multiplier=multiplier),
                        contract_specs=window_specs_maps[window],
                        funding_events=funding_by_window[window], now_epoch=now_epoch,
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

            universe_snapshot = [
                {
                    "rank": rank,
                    "symbol": str(row.get("symbol", "")),
                    "turnover_24h": float(row.get("turnover_24h", 0.0)),
                    "last_price": float(row.get("last_price", 0.0)),
                    "contract": dict(row.get("contract") or {}),
                }
                for rank, row in enumerate(universe_rows, 1)
            ]
            run_id = self.store.create_research_run(mode, {
                "symbols": symbols, "window": window, "strategy_version": STRATEGY_VERSION,
                "universe_snapshot_type": "CURRENT_TOP_TURNOVER_NOT_POINT_IN_TIME" if universe_rows else "SINGLE_SYMBOL",
                "universe_snapshot_time_utc": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
                "universe_snapshot": universe_snapshot,
                "survivorship_bias_warning": bool(universe_rows),
                "tp_scenario": scenario, "reward_risk_multiple": reward_risk,
                "config": {
                    "mode": cfg.mode, "margin_mode": cfg.margin_mode,
                    "starting_balance": cfg.starting_balance,
                    "risk_fraction": cfg.risk_fraction,
                    "initial_margin_per_position": cfg.initial_margin_per_position,
                    "max_leverage": cfg.max_leverage,
                    "max_concurrent_positions": cfg.max_concurrent_positions,
                    "max_open_risk_fraction": cfg.max_open_risk_fraction,
                    "max_total_notional_multiple": cfg.max_total_notional_multiple,
                    "maintenance_margin_fallback_fee": cfg.liquidation_fee_rate_fallback,
                    "taker_fee_rate": cfg.taker_fee_rate,
                    "slippage_bps_per_side": cfg.slippage_bps_per_side,
                    "cost_stress_multipliers": list(self.settings.cost_stress_multipliers),
                },
            })
            artifact_info = self._write_backtest_artifacts(result, job_id=job_id or run_id, run_id=run_id)
            result["artifacts"] = artifact_info
            self.store.finish_research_run(run_id, result)
            results.append(result)
            logger.info(
                "BACKTEST WINDOW COMPLETE | window=%s status=%s symbols=%d/%d bars=%d evaluations=%d candidates=%d entry_attempts=%d trades=%d rss_mb=%s rejects=%s elapsed_ms=%.0f",
                window, result.get("status"), result.get("symbols_eligible", 0), len(symbols),
                result.get("valid_candles", 0), result.get("strategy_evaluations", 0),
                result.get("candidate_signals", 0), result.get("entry_attempts", 0),
                result.get("completed_trades", 0), _process_rss_mb(), result.get("rejection_reasons", {}),
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
            if result.get("status") == "NOT_RUN_INSUFFICIENT_DATA_COVERAGE":
                lines.extend([
                    f"🧪 BACKTEST {window} — NOT RUN",
                    f"Eligible symbols: {result.get('symbols_eligible', 0)}/{result.get('symbols_tested', len(symbols))} "
                    f"({window_universe_coverage[window]:.2f}%; required {self.settings.backtest_min_universe_coverage_pct:.2f}%).",
                    *(result.get("warnings", [])[:2]),
                ])
                continue

            if result.get("status") == "INVALID_RECONCILIATION":
                lines.extend([
                    f"🧪 BACKTEST {window} — INVALID REPORT",
                    "Mathematical ledger reconciliation failed. Performance metrics are suppressed.",
                    f"Reconciliation: {result.get('reconciliation')}",
                ])
                continue
            lines.append(f"🧪 BACKTEST {window} · {scenario}")
            lines.append(f"Validation status: {result.get('validation_status', 'RESEARCH_ONLY')}")
            start_utc = datetime.fromtimestamp(int(result["start_time"]), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            end_utc = datetime.fromtimestamp(int(result["end_time"]), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"Requested UTC window: {start_utc} → {end_utc}")
            lines.append(f"Symbols {result['symbols_eligible']}/{result['symbols_tested']} · 1H bars {result['valid_candles']}")
            lines.append(
                f"Signals {result.get('candidate_signals', 0)} (LONG {result.get('long_signals', 0)} / SHORT {result.get('short_signals', 0)}) · "
                f"Entered {result.get('positions_entered', 0)} · Not entered {result.get('signals_not_entered', 0)}"
            )
            lines.append(
                f"Positions: max {result.get('max_observed_concurrent_positions', 0)}/"
                f"{result.get('configured_max_concurrent_positions', cfg.max_concurrent_positions)} observed/configured · "
                f"open at end {result.get('open_trades_at_end', 0)}"
            )
            lines.append(
                f"Evaluations {result.get('strategy_evaluations', 0)} · Candidates {result.get('candidate_signals', 0)} · Entries {result.get('entry_attempts', 0)}"
            )
            lines.append(
                f"🎯 TP {result.get('take_profit_hits', 0)} (L{result.get('long_take_profit_hits', 0)}/S{result.get('short_take_profit_hits', 0)}) · "
                f"🛑 SL {result.get('stop_loss_hits', 0)} (L{result.get('long_stop_loss_hits', 0)}/S{result.get('short_stop_loss_hits', 0)}) · "
                f"Liquidations {result.get('liquidation_exits', 0)} / events {result.get('liquidation_events', 0)} · "
                f"Time-stop {result.get('time_stop_exits', 0)} · Data-gap exits {result.get('data_gap_exits', 0)}"
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
                    f"Realized P&L ${result.get('realized_pnl', result['total_pnl']):+.2f} · "
                    f"Unrealized ${result.get('final_unrealized_pnl', 0.0):+.2f} · "
                    f"Equity ${result.get('ending_equity', result['ending_balance']):.2f} · DD {result['max_drawdown_pct']:.2f}%"
                )
                lines.append(
                    f"Total R {result.get('total_r', 0.0):+.2f} · Expectancy {result.get('expectancy_r_per_trade') if result.get('expectancy_r_per_trade') is not None else 'n/a'} R · "
                    f"Profit Factor {result.get('profit_factor')} · Max losing streak {result.get('max_losing_streak', 0)}"
                )
                lines.append(
                    f"Avg MAE/MFE {result.get('average_mae_r') if result.get('average_mae_r') is not None else 'n/a'} / "
                    f"{result.get('average_mfe_r') if result.get('average_mfe_r') is not None else 'n/a'} R · "
                    f"Avg signal/fill RR {result.get('average_signal_rr') if result.get('average_signal_rr') is not None else 'n/a'} / "
                    f"{result.get('average_actual_fill_rr') if result.get('average_actual_fill_rr') is not None else 'n/a'} · "
                    f"Avg holding {result.get('average_holding_hours') if result.get('average_holding_hours') is not None else 'n/a'}h"
                )
            if result.get("cost_sensitivity"):
                stress = " · ".join(
                    f"{item['cost_multiplier']:.1f}× ${item['total_pnl']:+.2f}"
                    for item in result["cost_sensitivity"]
                )
                lines.append(f"Cost stress: {stress}")
            if result.get("warnings"):
                lines.append("Data note: " + " · ".join(result["warnings"][:3]))
            if mode == "exchange_realistic" and funded_symbols_by_window.get(window):
                complete_funding = all(
                    funding_coverage_by_window[window].get(symbol, False)
                    for symbol in funded_symbols_by_window[window]
                )
                lines.append("Funding accounting: " + ("complete for simulated symbols in this window" if complete_funding else
                    "INCOMPLETE — observed settlements only; modeled P&L is not fully funding-adjusted"))

        lines.append("Live orders: OFF")
        lines.append(
            f"Margin: CROSS shared-wallet research model · per-position initial-margin cap ${cfg.initial_margin_per_position:.2f} · "
            f"max leverage {cfg.max_leverage}x · aggregate open-risk cap {cfg.max_open_risk_fraction:.1%} · "
            f"total notional cap {cfg.max_total_notional_multiple:.1f}x wallet."
        )
        lines.append("Liquidation: conservative 1H trade-price OHLC stress proxy, NOT historical Fair Price or exchange-exact partial liquidation.")
        lines.append("Margin tier/fee status: " + str(results[-1].get("risk_tier_status_counts", {})) +
                     f" · positions without verified liquidation fee: {results[-1].get('unverified_liquidation_fee_positions', 0)}")
        lines.append("Limitations: modeled fills/costs; no historical order-book replay, external positions or open-order margin replay. Live orders OFF.")
        lines.append(
            f"Universe: {universe_note} · Backtest cap: {cfg.max_concurrent_positions} positions · "
            f"Aggregate stop-risk cap: {cfg.max_open_risk_fraction:.1%} · Strategy: 1D → 12H → 4H → 1H"
        )
        total_ms = (time.perf_counter() - command_started) * 1000
        logger.info(
            "BACKTEST COMMAND COMPLETE | windows=%s requested_symbols=%d loaded_symbols=%d elapsed_ms=%.0f",
            windows, len(symbols), len(loaded), total_ms,
        )
        return CommandResult("\n".join(lines), event_type="BACKTEST_COMPLETED")

    def _write_backtest_artifacts(self, result: dict[str, Any], *, job_id: str, run_id: str) -> dict[str, Any]:
        """Persist the exact summary and closed-trade ledger on the worker's reports disk."""
        try:
            self.settings.reports_dir.mkdir(parents=True, exist_ok=True)
            end_stamp = datetime.fromtimestamp(int(result.get("end_time", 0) or 0), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe_id = "".join(ch for ch in str(job_id)[:16] if ch.isalnum() or ch in "-_ ").strip().replace(" ", "_") or "run"
            base = f"s1rfpc_{str(result.get('window', 'unknown')).lower()}_{safe_id}_{end_stamp}"
            json_path = self.settings.reports_dir / f"{base}.json"
            csv_path = self.settings.reports_dir / f"{base}_trades.csv"
            txt_path = self.settings.reports_dir / f"{base}.txt"
            json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            trades = list(result.get("trades") or [])
            if trades:
                fieldnames = sorted({key for row in trades for key in row})
                with csv_path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(trades)
            else:
                csv_path.write_text("no closed trades\n", encoding="utf-8")
            txt_path.write_text(
                "PAK TRADING ACADEMY — S1-RFPC BACKTEST\n"
                f"Job: {job_id}\nResearch run: {run_id}\n"
                f"Status: {result.get('status')}\nValidation: {result.get('validation_status', 'RESEARCH_ONLY')}\n"
                f"Window: {result.get('window')}\n"
                f"UTC: {result.get('start_time')} → {result.get('end_time')}\n"
                f"Symbols: {result.get('symbols_eligible')}/{result.get('symbols_tested')}\n"
                f"Signals: {result.get('candidate_signals')} (LONG {result.get('long_signals')}, SHORT {result.get('short_signals')})\n"
                f"Entered: {result.get('positions_entered')} | Closed: {result.get('completed_trades')} | Open: {result.get('open_trades_at_end')}\n"
                f"TP: {result.get('take_profit_hits')} | SL: {result.get('stop_loss_hits')} | Liquidations: {result.get('liquidation_exits')}\n"
                f"Realized P&L: {result.get('realized_pnl')} | Unrealized: {result.get('final_unrealized_pnl')} | Equity: {result.get('ending_equity')}\n"
                f"Total R: {result.get('total_r')} | Expectancy R: {result.get('expectancy_r_per_trade')} | PF: {result.get('profit_factor')}\n"
                f"Reconciliation: {result.get('reconciliation')}\n\n"
                "This is a historical simulation, not an exchange execution record.\n",
                encoding="utf-8",
            )
            return {"status": "SAVED", "json": str(json_path), "trades_csv": str(csv_path), "text": str(txt_path)}
        except Exception as exc:
            logger.exception("BACKTEST ARTIFACT WRITE FAILED | job_id=%s", job_id)
            return {"status": "FAILED", "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

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
    """Compatibility wrapper around the shared fail-closed contract validator."""
    return validate_usdt_perpetual_contract(spec, margin_mode=margin_mode)
