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

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from app.alerts import AlertService
from app.analysis import analyze_series, format_analysis
from app.backtest import BacktestConfig, run_backtest
from app.charting import render_candle_chart
from app.commands import Command, CommandError, HELP_TEXT, parse_command
from app.config import Settings
from app.market.candles import Candle, INTERVAL_SECONDS, aggregate_12h_from_4h
from app.mexc.client import MexcClient, MexcError
from app.services.market_data import MarketDataService, SymbolData
from app.storage import Store, canonical_json
from app.symbols import normalize_symbol
from app.whatsapp import WhatsAppCloud, WhatsAppPolicyBlocked, sender_key

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CommandResult:
    text: str
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (path, image|document)
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
            source_ts = ticker.get("timestamp", ticker.get("time"))
            if source_ts is not None:
                try:
                    source_stamp = int(float(source_ts))
                    if source_stamp > 100_000_000_000:
                        source_stamp //= 1000
                    source_time = datetime.fromtimestamp(source_stamp, timezone.utc).isoformat(timespec="seconds")
                except (TypeError, ValueError, OverflowError, OSError):
                    source_time = "unparseable"
            else:
                source_time = "not provided by response"
            return CommandResult(f"MEXC FUTURES PRICE — {symbol}\nLast traded price: {price:.10g}\nSource timestamp: {source_time}\nFetched: {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC\nSource: MEXC Futures ticker. This is a market observation, not an order fill.")
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
            last_closed = datetime.fromtimestamp(candles[-1].close_time, timezone.utc).isoformat(timespec="seconds")
            caption = (
                f"MEXC FUTURES CHART — {symbol} · {timeframe}\n"
                f"Completed candles: {len(candles)} | Latest candle close: {last_closed} UTC\n"
                "Indicators: EMA 21/50/100/200, Volume, RSI 14 and confirmed swing levels.\n"
                "Signal framework: completed 1D → 12H → 4H → 1H candles only."
            )
            return CommandResult(caption, [(str(path), "image")], "CHART_RENDERED")
        if name == "ANALYZE":
            symbol = args["symbol"]
            data = await self.market_data.fetch_symbol(symbol, window_days=15, daily_warmup_days=400, extra_intraday_warmup_days=45)
            report = analyze_series(symbol, data.timeframes, data_warnings=data.warnings)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            json_path = self.settings.reports_dir / f"analysis_{symbol}_{stamp}.json"
            text_path = self.settings.reports_dir / f"analysis_{symbol}_{stamp}.txt"
            json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
            analysis_text = format_analysis(report)
            text_path.write_text(analysis_text + "\n", encoding="utf-8")
            return CommandResult(analysis_text, [(str(text_path), "document"), (str(json_path), "json")], "ANALYSIS_COMPLETED")
        if name == "SCAN":
            results: list[str] = []
            for symbol in self.settings.default_symbols:
                try:
                    data = await self.market_data.fetch_symbol(symbol, window_days=10, daily_warmup_days=400, extra_intraday_warmup_days=45)
                    report = analyze_series(symbol, data.timeframes, data_warnings=data.warnings)
                    results.append(f"{symbol}: {report['regime']} / {report['signal']} ({', '.join(report['rejection_reasons'][:2])})")
                    self.store.append_event("SCAN_RESULT", report, symbol=symbol,
                        idempotency_key=f"scan:{symbol}:{report['decision_time']}")
                except Exception as exc:
                    results.append(f"{symbol}: DATA/ANALYSIS ERROR ({type(exc).__name__})")
            return CommandResult("MEXC FUTURES SCAN\n" + "\n".join(results) + "\nHypotheses only; no live orders.", event_type="SCAN_COMPLETED")
        if name == "BACKTEST":
            # Every public BACKTEST command uses the selected 1D → 12H → 4H → 1H engine.
            # The legacy 3-minute RSI/EMA simulator is not part of this command path.
            async with self._backtest_lock:
                return await self._run_backtest(args)
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
                events = self.store.recent_events(20, "SCAN_RESULT")
                if not events:
                    return CommandResult("No recorded scan results yet.")
                return CommandResult("RECENT SCAN RESULTS\n" + "\n".join(f"{x['created_at']} | {x['symbol']} | {json.loads(x['payload_json']).get('signal', 'n/a')}" for x in events[:15]))
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

    async def _run_backtest(self, args: dict[str, Any]) -> CommandResult:
        """Backtest the selected 1D → 12H → 4H → 1H strategy on verified completed bars."""
        symbols = [args["symbol"]] if args.get("symbol") else list(self.settings.default_symbols)
        windows = list(args.get("windows") or ["7D"])
        days_by_window = {"1D": 1, "3D": 3, "7D": 7, "30D": 30, "60D": 60, "90D": 90, "180D": 180, "365D": 365}
        try:
            now_epoch = await self.mexc.server_time()
            clock_warning: list[str] = []
        except Exception as exc:
            now_epoch = int(time.time())
            clock_warning = [f"Exchange server time was unavailable ({type(exc).__name__}); local UTC time was used only to bound the data window."]
        longest = max(days_by_window[w] for w in windows)
        loaded: dict[str, SymbolData] = {}
        data_load_errors: dict[str, str] = {}

        async def load_one(symbol: str):
            return symbol, await self.market_data.fetch_symbol(
                symbol, window_days=longest, daily_warmup_days=400,
                extra_intraday_warmup_days=45, now_epoch=now_epoch,
            )

        load_results = await asyncio.gather(*(load_one(s) for s in symbols), return_exceptions=True)
        for symbol, item in zip(symbols, load_results):
            if isinstance(item, Exception):
                data_load_errors[symbol] = f"{type(item).__name__}: {str(item)[:160]}"
                self.store.append_event("BACKTEST_DATA_LOAD_FAILED", {
                    "symbol": symbol, "error_type": type(item).__name__, "window_days": longest,
                }, symbol=symbol, idempotency_key=f"backtest-load-failed:{symbol}:{now_epoch // 60}")
                logger.warning("Backtest market data failed for %s (%s)", symbol, type(item).__name__)
            else:
                loaded[symbol] = item[1]

        series_map = {s: d.timeframes for s, d in loaded.items()}
        # Include empty entries to report an explicit no-data result rather than silently
        # pretending that a failed API request was a losing or breakeven strategy.
        if not series_map:
            series_map = {s: {} for s in symbols}
        specs = {s: d.contract for s, d in loaded.items() if d.contract}
        funding: dict[str, list[dict[str, Any]]] = {}
        funding_missing_symbols: list[str] = []
        mode = args.get("mode", "exchange_realistic")
        if mode == "exchange_realistic":
            funding_start = now_epoch - (longest + 45) * 86400

            async def load_funding(symbol: str):
                return symbol, await self.mexc.funding_history_window(symbol, start=funding_start, end=now_epoch)

            funding_results = await asyncio.gather(*(load_funding(s) for s in loaded), return_exceptions=True)
            for symbol, item in zip(loaded, funding_results):
                if isinstance(item, Exception):
                    funding[symbol] = []
                    funding_missing_symbols.append(symbol)
                    logger.warning("Funding history fetch failed for %s (%s)", symbol, type(item).__name__)
                else:
                    funding[symbol] = item[1]
                    if not item[1]:
                        funding_missing_symbols.append(symbol)

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
        results: list[dict[str, Any]] = []
        data_warnings = list(clock_warning)
        data_warnings.extend(
            f"{s}: market-data load failed ({error}); this symbol was not simulated."
            for s, error in data_load_errors.items()
        )
        for symbol, data in loaded.items():
            data_warnings.extend(f"{symbol}: {warning}" for warning in data.warnings)
        if mode == "exchange_realistic" and funding_missing_symbols:
            data_warnings.extend(
                f"{symbol}: funding history is missing/empty; realized P&L excludes unknown funding."
                for symbol in funding_missing_symbols
            )

        for window in windows:
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
            sensitivity: list[dict[str, Any]] = []
            if mode == "exchange_realistic":
                multipliers = sorted(set((1.0, *self.settings.cost_stress_multipliers)))
                for multiplier in multipliers:
                    stress_cfg = replace(cfg, cost_multiplier=multiplier)
                    stress_result = result if multiplier == 1.0 else await asyncio.to_thread(
                        run_backtest, series_map, window=window, config=stress_cfg,
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
                "config": {"mode": cfg.mode, "tp_pct": cfg.tp_pct, "sl_pct": cfg.sl_pct,
                           "risk_fraction": cfg.risk_fraction, "taker_fee_rate": cfg.taker_fee_rate,
                           "slippage_bps_per_side": cfg.slippage_bps_per_side,
                           "cost_stress_multipliers": list(self.settings.cost_stress_multipliers)},
            })
            self.store.finish_research_run(run_id, result)
            stamp_artifact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            csv_path = self.settings.reports_dir / f"backtest_{window}_{scenario}_{stamp_artifact}_trades.csv"
            csv_fields = ["symbol", "side", "entry_time", "exit_time", "entry_price", "exit_price",
                          "exit_reason", "pnl", "realised_r", "risk_amount", "candidate_id", "mode",
                          "fees", "funding_pnl", "slippage_cost"]
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=csv_fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(result.get("trades", []))

            curve_path = self.settings.reports_dir / f"backtest_{window}_{scenario}_{stamp_artifact}_equity.png"
            fig, axis = plt.subplots(figsize=(10, 4.8), dpi=140, facecolor="#0b0f14")
            axis.set_facecolor("#10161f")
            axis.tick_params(colors="#b8c3d1", labelsize=8)
            for spine in axis.spines.values():
                spine.set_color("#283241")
            axis.set_title(f"Paper Backtest Equity — {window} — TP {scenario}", color="#e2e8f0", loc="left")
            if result.get("status") == "NOT_RUN_NO_DATA":
                axis.text(0.5, 0.5, "NOT RUN — NO VERIFIED CANDLE DATA", transform=axis.transAxes,
                          ha="center", va="center", color="#f59e0b", fontsize=11, fontweight="bold")
            elif result.get("status") == "NOT_RUN_INSUFFICIENT_HISTORY":
                axis.text(0.5, 0.5, "NOT RUN — INSUFFICIENT STRATEGY WARM-UP", transform=axis.transAxes,
                          ha="center", va="center", color="#f59e0b", fontsize=11, fontweight="bold")
            elif result.get("status") == "COMPLETED_NO_TRADES":
                axis.text(0.5, 0.5, "NO QUALIFYING CLOSED TRADES\nNo expectancy conclusion", transform=axis.transAxes,
                          ha="center", va="center", color="#b8c3d1", fontsize=11, fontweight="bold")
            else:
                curve = result.get("equity_curve", [])
                if len(curve) > 1200:
                    stride = max(1, len(curve) // 1200)
                    curve = curve[::stride]
                    if result.get("equity_curve"):
                        curve = [*curve, result["equity_curve"][-1]]
                axis.plot(range(len(curve)), curve, color="#22c55e", linewidth=1.3, label="Simulated equity")
                axis.axhline(cfg.starting_balance, color="#8b98aa", linestyle="--", linewidth=0.8,
                             alpha=0.75, label="Starting wallet")
                axis.set_ylabel("Equity (quote currency)", color="#b8c3d1", fontsize=8)
                axis.set_xlabel("Hourly simulation steps", color="#b8c3d1", fontsize=8)
                axis.legend(loc="upper left", frameon=False, labelcolor="#d7dee8", fontsize=8)
            axis.grid(True, color="#283241", linewidth=0.55, alpha=0.65)
            fig.tight_layout()
            try:
                fig.savefig(curve_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
            finally:
                plt.close(fig)
            result["artifacts"] = {"trades_csv": str(csv_path), "equity_curve_png": str(curve_path)}
            results.append(result)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_path = self.settings.reports_dir / f"backtest_{mode}_{stamp}.json"
        text_path = self.settings.reports_dir / f"backtest_{mode}_{stamp}.txt"
        json_path.write_text(json.dumps({
            "mode": mode, "symbols": symbols, "strategy": "S1-RFPC 1D-12H-4H-1H",
            "tp_scenario": scenario, "reward_risk_multiple": reward_risk, "results": results,
        }, indent=2, sort_keys=True), encoding="utf-8")
        lines = [
            f"BACKTEST — {mode.upper()}",
            "Strategy: 1D regime → 12H structure → 4H setup → 1H trigger",
            f"TP scenario: {scenario} ({reward_risk:.1f}R gross target before costs)",
            f"Symbols requested: {', '.join(symbols)}",
            f"Symbols with retrieved history: {', '.join(sorted(loaded)) if loaded else 'none'}",
            f"Starting simulated equity per run: ${cfg.starting_balance:.2f}",
            "Only completed 1D, 12H, 4H and 1H candles are used. Live trading: DISABLED.",
        ]
        if data_load_errors:
            lines.append("DATA LOAD ERRORS: " + "; ".join(f"{symbol}={error}" for symbol, error in data_load_errors.items()))
        for result in results:
            label = result["window"]
            status = result.get("status", "COMPLETED")
            if status == "NOT_RUN_NO_DATA":
                lines.append(f"{label}: NOT RUN — no completed historical candles were available; no performance result.")
                continue
            if status == "NOT_RUN_INSUFFICIENT_HISTORY":
                lines.append(f"{label}: NOT RUN — insufficient 1D EMA200/1H warm-up; no performance result.")
                continue
            if status == "COMPLETED_NO_TRADES":
                lines.append(
                    f"{label}: NO QUALIFYING CLOSED TRADES | 1H bars in test={result['valid_candles']} "
                    f"| eligible symbols={result['symbols_eligible']}/{result['symbols_tested']} | "
                    "no trade expectancy can be inferred from the unchanged wallet balance."
                )
            else:
                wr = "n/a" if result["win_rate_pct"] is None else f"{result['win_rate_pct']:.2f}%"
                lines.append(
                    f"{label}: trades={result['completed_trades']} open={result['open_trades_at_end']} "
                    f"W/L={result['wins']}/{result['losses']} win-rate={wr} "
                    f"net P&L=${result['total_pnl']:.2f} ending=${result['ending_balance']:.2f} "
                    f"maxDD={result['max_drawdown_pct']:.2f}%"
                )
            if result.get("cost_sensitivity"):
                stress_line = ", ".join(f"{x['cost_multiplier']:.1f}x=${x['total_pnl']:.2f}" for x in result["cost_sensitivity"])
                lines.append(f"  Cost-stress net P&L: {stress_line}")
        lines.append(
            "Modeled P&L is not observed execution. Funding gaps, account-specific fees, contract units, "
            "slippage, liquidation and intra-candle price ordering can change results. See the attached report JSON."
        )
        text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        attachments: list[tuple[str, str]] = [(str(text_path), "document"), (str(json_path), "document")]
        for result in results:
            artifacts = result.get("artifacts", {})
            if artifacts.get("trades_csv"):
                attachments.append((str(artifacts["trades_csv"]), "document"))
            if artifacts.get("equity_curve_png"):
                attachments.append((str(artifacts["equity_curve_png"]), "image"))
        return CommandResult("\n".join(lines), attachments, "BACKTEST_COMPLETED")

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


def _validate_backtest_contract_spec(spec: dict[str, Any]) -> tuple[bool, str]:
    """Require verified active contract metadata before simulating order sizes."""
    state = spec.get("state")
    try:
        if state is None or int(state) != 0:
            return False, "CONTRACT_NOT_VERIFIED_ACTIVE"
    except (TypeError, ValueError, OverflowError):
        return False, "CONTRACT_STATE_INVALID"

    required = {
        "contractSize": ("contractSize", "contract_size"),
        "volUnit": ("volUnit", "volumeUnit", "quantityStep"),
        "minVol": ("minVol", "minVolume", "minOrderQty"),
        "maxVol": ("maxVol", "maxVolume", "maxOrderQty"),
    }
    for label, keys in required.items():
        value = _first_number(spec, *keys)
        if value is None or value <= 0:
            return False, f"CONTRACT_METADATA_INVALID:{label}"
    leverage = _first_number(spec, "countryConfigContractMaxLeverage", "maxLeverage", "max_leverage")
    if leverage is None or leverage < 1:
        return False, "CONTRACT_METADATA_INVALID:MAX_LEVERAGE"
    return True, "OK"
