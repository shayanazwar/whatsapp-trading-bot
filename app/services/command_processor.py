"""Command dispatch for the official WhatsApp adapter; all processing is deterministic."""
from __future__ import annotations

import asyncio
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
from app.backtest_rsi_ema import (RsiEmaBacktestConfig, format_backtest_whatsapp, run_backtest_rsi_ema, write_backtest_artifacts)
from app.strategy.rsi_ema import RsiEmaConfig, evaluate_signals
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


@dataclass(slots=True)
class CommandResult:
    text: str
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (path, image); documents are disabled
    event_type: str = "COMMAND_COMPLETED"
    idempotency_key: str | None = None


class CommandProcessor:
    def __init__(self, settings: Settings, store: Store, mexc: MexcClient,
                 whatsapp: WhatsAppCloud | None = None) -> None:
        self.settings = settings
        self.store = store
        self.mexc = mexc
        self.whatsapp = whatsapp
        self.market_data = MarketDataService(mexc, close_grace_seconds=settings.candle_close_grace_seconds,
                                             price_series=settings.mexc_price_series)
        self.alerts = AlertService(store)
        self._backtest_lock = asyncio.Lock()
        self.settings.reports_dir.mkdir(parents=True, exist_ok=True)

    async def process_text(self, sender: str, text: str, *, received_timestamp: int | None = None,
                           enforce_whatsapp_policy: bool = True) -> CommandResult:
        # Authorize before parsing to avoid exposing command grammar to unauthorized senders.
        allowed = {sender_key(x) for x in self.settings.whatsapp_allowed_senders}
        if enforce_whatsapp_policy and (not allowed or sender_key(sender) not in allowed):
            raise CommandError("Sender is not authorized")
        if received_timestamp is not None and int(time.time()) - received_timestamp > 300:
            raise CommandError("This message is too old to execute. Send the command again.")
        command = parse_command(text)
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
            async with self._backtest_lock:
                return await self._run_rsi_ema_backtest(args)
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
                events = self.store.recent_events(250, "SCAN_RESULT_3M")
                if not events:
                    return CommandResult("No TOP-100 3M scan results recorded yet. Run SCAN or check the scanner worker.")
                decoded = [(event, json.loads(event["payload_json"])) for event in events]
                known_slots = [int(payload["scan_slot"]) for _, payload in decoded if payload.get("scan_slot") is not None]
                latest_slot = max(known_slots) if known_slots else None
                latest = [(event, payload) for event, payload in decoded
                          if latest_slot is None or payload.get("scan_slot") == latest_slot]
                signal_rows = [(event, payload) for event, payload in latest if payload.get("signal")]
                signal_rows.sort(key=lambda pair: float(pair[1].get("turnover_24h", 0.0)), reverse=True)
                lines = ["LATEST COMPLETED 3M SCAN"]
                if signal_rows:
                    lines.append(f"Signals found: {len(signal_rows)}")
                    for event, payload in signal_rows[:15]:
                        signal = payload["signal"]
                        lines.append(
                            f"{event['symbol']} {signal.get('direction', 'SIGNAL')} @ "
                            f"{float(signal.get('close', 0.0)):.8g} · RSI {float(signal.get('rsi', 0.0)):.1f}"
                        )
                else:
                    error_count = sum(payload.get("status") == "DATA_ERROR" for _, payload in latest)
                    lines.append(f"No qualifying signal · checked {len(latest)} symbols · data errors {error_count}")
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
        started = time.perf_counter()
        now = await self.mexc.server_time()
        scan_slot = (now - self.settings.candle_close_grace_seconds) // 180
        universe = await self._load_top_universe(self.settings.mexc_universe_top_n)
        semaphore = asyncio.Semaphore(8)
        cfg = self._rsi_ema_config()

        async def scan_one(item: dict[str, Any]):
            symbol = item["symbol"]
            async with semaphore:
                try:
                    bars = await self.mexc.get_klines_window(
                        symbol, "3M", start=now - 450 * 180, end=now, now_epoch=now,
                        close_grace_seconds=self.settings.candle_close_grace_seconds,
                    )
                    bars = self._apply_selected_price_series(bars)
                    if len(bars) < max(cfg.ema_slow, cfg.rsi_period + 1, cfg.volume_period + 1) + 2:
                        return (symbol, item["turnover_24h"], "INSUFFICIENT_HISTORY", None, None)
                    signals = evaluate_signals(bars, cfg)
                    latest_signal = signals[-1] if signals and signals[-1].timestamp == bars[-1].close_time else None
                    return (symbol, item["turnover_24h"], "SIGNAL" if latest_signal else "NO_SIGNAL",
                            latest_signal.as_dict() if latest_signal else None, None)
                except Exception as exc:
                    return (symbol, item["turnover_24h"], "DATA_ERROR", None,
                            f"{type(exc).__name__}: {str(exc)[:100]}")

        rows = await asyncio.gather(*(scan_one(item) for item in universe))
        rows.sort(key=lambda x: (-x[1], x[0]))
        signals = [row for row in rows if row[2] == "SIGNAL"]
        errors = [row for row in rows if row[2] == "DATA_ERROR"]
        for symbol, turnover, status, signal, error in rows:
            self.store.append_event("SCAN_RESULT_3M", {
                "symbol": symbol, "turnover_24h": turnover, "status": status,
                "signal": signal, "error": error, "timeframe": "3M",
                "scan_slot": scan_slot,
            }, symbol=symbol, idempotency_key=f"scan3m:{symbol}:{scan_slot}")
        lines = [
            "🔎 MEXC TOP-100 FUTURES SCAN · 3M",
            f"Scanned {len(rows)}/{len(universe)} · Signals {len(signals)} · Data errors {len(errors)}",
        ]
        if signals:
            lines.append("LATEST SIGNALS")
            for symbol, turnover, _, signal, _ in signals[:20]:
                lines.append(f"{symbol} {signal.get('direction', 'SIGNAL')} · ${signal['close']:.8g} · RSI {signal['rsi']:.1f}")
        else:
            lines.append("No qualifying signal on the latest completed 3M candle.")
        if errors:
            lines.append("Data errors: " + ", ".join(row[0] for row in errors[:8]))
        lines.append(f"Ranked by 24H USDT turnover · {time.perf_counter()-started:.1f}s · Live orders OFF")
        return CommandResult("\n".join(lines), event_type="SCAN_3M_COMPLETED")

    async def _run_rsi_ema_backtest(self, args: dict[str, Any]) -> CommandResult:
        """Run a completed-candle, top-universe 3M backtest and persist its full audit artifacts."""
        started = time.perf_counter()
        days_map = {"1D": 1, "7D": 7, "30D": 30, "60D": 60, "90D": 90}
        # `window` is the canonical parsed argument. Never silently default to 1D/7D:
        # a missing period indicates a parser/deployment mismatch and must fail closed.
        window = str(args.get("window") or "").strip().upper()
        legacy_windows = args.get("windows")
        if not window and isinstance(legacy_windows, list) and len(legacy_windows) == 1:
            window = str(legacy_windows[0]).strip().upper()
        if window and isinstance(legacy_windows, list) and len(legacy_windows) == 1:
            legacy_window = str(legacy_windows[0]).strip().upper()
            if legacy_window != window:
                raise CommandError(
                    f"Conflicting backtest periods received: window={window}, windows={legacy_window}. "
                    "Send BACKTEST 1D or BACKTEST 7D again."
                )
        if window not in days_map:
            raise CommandError("Backtest period missing or invalid. Send BACKTEST 1D or BACKTEST 7D.")
        windows = [window]
        longest = days_map[window]
        if not args.get("symbol") and longest > 7:
            raise CommandError(
                "Top-100 3M backtests are limited to 1D/7D. Use BACKTEST SYMBOL 30D/60D/90D for a longer test."
            )

        now = await self.mexc.server_time()
        if args.get("symbol"):
            contract = await self.mexc.contract_detail(args["symbol"])
            if not contract:
                raise CommandError("The requested symbol is not an active MEXC Futures contract.")
            universe = [{"symbol": args["symbol"], "turnover_24h": 0.0, "contract": contract}]
        else:
            universe = await self._load_top_universe(self.settings.mexc_universe_top_n)

        data_semaphore = asyncio.Semaphore(8)
        loaded: dict[str, list[Candle]] = {}
        specs: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        history_start = now - longest * 86400 - 450 * 180

        async def load_one(item: dict[str, Any]):
            symbol = item["symbol"]
            async with data_semaphore:
                bars = await self.mexc.get_klines_window(
                    symbol, "3M", start=history_start, end=now, now_epoch=now,
                    close_grace_seconds=self.settings.candle_close_grace_seconds,
                )
                return symbol, self._apply_selected_price_series(bars), item["contract"]

        results = await asyncio.gather(*(load_one(item) for item in universe), return_exceptions=True)
        for item, result in zip(universe, results):
            if isinstance(result, Exception):
                failures.append(f"{item['symbol']}:{type(result).__name__}")
                continue
            symbol, bars, contract = result
            valid, reason = _validate_backtest_contract_spec(
                contract, margin_mode=self.settings.backtest_margin_mode,
            )
            if not valid:
                failures.append(f"{symbol}:{reason}")
                continue
            if bars:
                loaded[symbol] = bars
                specs[symbol] = contract
        if not loaded:
            raise MexcError("No verified completed 3M candles and valid contract metadata were loaded.")

        strategy = self._rsi_ema_config()
        preliminary_config = RsiEmaBacktestConfig(
            starting_equity=self.settings.default_starting_balance,
            initial_margin=self.settings.initial_margin_usdt,
            max_leverage=self.settings.max_leverage,
            net_take_profit_usd=self.settings.net_take_profit_usdt,
            requested_stop_loss_usd=self.settings.requested_stop_loss_usdt,
            taker_fee_rate=self.settings.exchange_taker_fee_rate,
            slippage_bps_per_side=self.settings.backtest_slippage_bps_per_side,
            margin_mode=self.settings.backtest_margin_mode,
            max_holding_minutes=self.settings.backtest_max_holding_minutes,
            strategy=strategy,
        )
        cutoff = now - longest * 86400
        signal_symbols: set[str] = set()
        for symbol, bars in loaded.items():
            try:
                candidates = evaluate_signals(bars, strategy)
                if any(
                    signal.timestamp > cutoff
                    and signal.candle_index + 1 < len(bars)
                    and bars[signal.candle_index + 1].open_time >= cutoff
                    for signal in candidates
                ):
                    signal_symbols.add(symbol)
            except Exception as exc:
                logger.warning("BACKTEST SIGNAL PREFLIGHT FAILED | symbol=%s error=%s", symbol, type(exc).__name__)

        support_semaphore = asyncio.Semaphore(6)
        funding_events: dict[str, list[dict[str, Any]]] = {}
        fair_price_bars: dict[str, list[Candle]] = {}
        funding_price_bars: dict[str, list[Candle]] = {}
        funding_missing: list[str] = []
        funding_partial: list[str] = []
        fair_price_missing: list[str] = []

        async def load_trade_support(symbol: str):
            async with support_semaphore:
                funding_task = self.mexc.funding_history_window_with_coverage(
                    symbol, start=cutoff, end=now, max_pages=4,
                )
                fair_task = self.mexc.get_fair_price_klines_window(
                    symbol, "1M", start=history_start, end=now,
                    now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds,
                )
                funding_result, fair_result = await asyncio.gather(
                    funding_task, fair_task, return_exceptions=True,
                )
                return symbol, funding_result, fair_result

        support_results = await asyncio.gather(
            *(load_trade_support(symbol) for symbol in sorted(signal_symbols)),
            return_exceptions=True,
        )
        for item in support_results:
            if isinstance(item, Exception):
                logger.warning("BACKTEST SUPPORT DATA FAILED | error_type=%s detail=%s", type(item).__name__, str(item)[:140])
                continue
            symbol, funding_result, fair_result = item
            if isinstance(funding_result, Exception):
                funding_events[symbol] = []
                funding_missing.append(symbol)
                logger.warning("BACKTEST FUNDING LOAD FAILED | symbol=%s error_type=%s", symbol, type(funding_result).__name__)
            else:
                funding_rows, funding_coverage_complete = funding_result
                funding_events[symbol] = list(funding_rows)
                if not funding_rows:
                    funding_missing.append(symbol)
                elif not funding_coverage_complete:
                    funding_partial.append(symbol)
            if isinstance(fair_result, Exception):
                fair_price_missing.append(symbol)
                logger.warning("BACKTEST FAIR PRICE LOAD FAILED | symbol=%s error_type=%s", symbol, type(fair_result).__name__)
            else:
                if fair_result:
                    funding_price_bars[symbol] = list(fair_result)
                derived_fair = aggregate_3m_from_1m(fair_result)
                if derived_fair:
                    fair_price_bars[symbol] = derived_fair
                else:
                    fair_price_missing.append(symbol)

        config = preliminary_config
        report = await asyncio.to_thread(
            run_backtest_rsi_ema,
            loaded,
            window_days=longest,
            config=config,
            contract_specs=specs,
            funding_events=funding_events,
            liquidation_candles_by_symbol=fair_price_bars,
            funding_price_candles_by_symbol=funding_price_bars,
            now_epoch=now,
            close_grace_seconds=self.settings.candle_close_grace_seconds,
        )
        # Preserve and cross-check the requested horizon all the way into the report.
        if int(report.get("window_days", -1)) != longest:
            raise RuntimeError(
                f"Backtest horizon mismatch: requested {window} ({longest}D), "
                f"engine returned {report.get('window_days')}D"
            )
        report["window"] = window
        report["window_days"] = longest
        report["requested_window"] = window
        report["requested_start_epoch"] = cutoff
        report["requested_end_epoch"] = now
        report["requested_start_utc"] = datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        report["requested_end_utc"] = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        report["coins_tested"] = len(universe)
        report["coins_with_data"] = len(loaded)
        report["loaded_3m_candles_including_warmup"] = sum(len(v) for v in loaded.values())
        observed_window_bars = sum(report.get("data_quality", {}).get("bar_counts", {}).values())
        expected_per_symbol = int(report.get("data_quality", {}).get("expected_completed_3m_bars_per_symbol", 0))
        expected_total = expected_per_symbol * len(loaded)
        report["observed_window_3m_bars"] = observed_window_bars
        report["expected_window_3m_bars"] = expected_total
        report["window_candle_coverage_pct"] = (
            min(100.0, observed_window_bars / expected_total * 100.0) if expected_total else None
        )
        first_window_stamps = report.get("data_quality", {}).get("first_completed_bar_by_symbol", {}).values()
        last_window_stamps = report.get("data_quality", {}).get("last_completed_bar_by_symbol", {}).values()
        first_window_stamp = min(first_window_stamps, default=None)
        last_window_stamp = max(last_window_stamps, default=None)
        report["actual_data_start_utc"] = (
            datetime.fromtimestamp(first_window_stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if first_window_stamp is not None else "n/a"
        )
        report["actual_data_end_utc"] = (
            datetime.fromtimestamp(last_window_stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if last_window_stamp is not None else "n/a"
        )
        report["data_errors"] = list(failures)
        # The engine flags missing support data for symbols that actually opened a
        # trade, not every market that merely produced a signal.
        report["data_quality"]["support_data_requested_symbols"] = sorted(signal_symbols)
        report["data_quality"]["funding_partial_coverage_symbols"] = sorted(set(funding_partial))
        report["data_quality"]["support_data_fetch_errors"] = {
            "funding": sorted(set(funding_missing)),
            "fair_price": sorted(set(fair_price_missing)),
        }

        # Microseconds prevent same-second commands from overwriting each other's audit files.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        artifact_paths = write_backtest_artifacts(
            report, self.settings.reports_dir, prefix=f"backtest_3m_{window.lower()}_{stamp}",
        )
        run_id = self.store.create_research_run("rsi_ema_3m_cross_margin", {
            "symbols": sorted(loaded), "window": window,
            "strategy_version": self.settings.strategy_version,
            "universe": "top_100_by_24h_turnover" if not args.get("symbol") else "single_symbol",
            "margin_mode": config.margin_mode,
            "starting_equity": config.starting_equity,
            "initial_margin": config.initial_margin,
            "max_leverage": config.max_leverage,
            "net_take_profit_usd": config.net_take_profit_usd,
            "requested_stop_loss_usd": config.requested_stop_loss_usd,
        })
        report["artifact_paths"] = artifact_paths
        self.store.finish_research_run(run_id, report)

        summary = format_backtest_whatsapp(report, elapsed_seconds=time.perf_counter() - started)
        if failures:
            summary += f"\n\n⚠️ Data/metadata errors: {len(failures)} ({', '.join(failures[:8])})"
        summary += f"\n📁 Audit files saved: {Path(artifact_paths['json']).name}, {Path(artifact_paths['csv']).name}"
        logger.info(
            "BACKTEST 3M COMPLETE | requested_window=%s window_days=%d start_utc=%s end_utc=%s universe=%d data=%d candles=%d signals=%d entered=%d resolved=%d unresolved=%d liquidations=%d elapsed_s=%.1f margin_mode=%s",
            window, longest, report["requested_start_utc"], report["requested_end_utc"],
            len(universe), len(loaded), report["loaded_3m_candles_including_warmup"],
            report["signals"], report["trades_opened"], report["resolved"],
            report["open_unresolved"], report["liquidation_exits"], time.perf_counter() - started,
            config.margin_mode,
        )
        return CommandResult(summary, event_type="BACKTEST_3M_COMPLETED")

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
