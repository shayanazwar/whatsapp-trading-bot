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
from app.charting import render_5m_chart
from app.commands import Command, CommandError, HELP_TEXT, parse_command
from app.config import Settings
from app.market.candles import Candle
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
        if enforce_whatsapp_policy and not self.settings.whatsapp_policy_gate_open:
            raise WhatsAppPolicyBlocked("WhatsApp command processing is disabled until written Meta policy clearance covers the exact crypto use case.")
        allowed = {sender_key(x) for x in self.settings.whatsapp_allowed_senders}
        if enforce_whatsapp_policy and (not allowed or sender_key(sender) not in allowed):
            raise CommandError("Sender is not authorized")
        if received_timestamp is not None and int(time.time()) - received_timestamp > 300:
            raise CommandError("This message is too old to execute. Send the command again.")
        command = parse_command(text)
        result = await self.execute(command)
        self.store.append_event("WHATSAPP_COMMAND_PROCESSED" if enforce_whatsapp_policy else "LOCAL_COMMAND_PROCESSED",
            {"sender": sender_key(sender), "command": command.name, "args": command.args,
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
                f"WhatsApp policy gate: {'OPEN BY CONFIG' if self.settings.whatsapp_policy_gate_open else 'BLOCKED'}\n"
                "No live MEXC order path is enabled."
            )
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
            now = int(time.time())
            candles = await self.mexc.get_klines_window(symbol, "5M", start=now - 500 * 300, end=now,
                now_epoch=now, close_grace_seconds=self.settings.candle_close_grace_seconds, max_bars_per_request=500)
            if self.settings.mexc_price_series == "real":
                from dataclasses import replace
                if any(any(v is None for v in (c.real_open, c.real_high, c.real_low, c.real_close)) for c in candles):
                    raise MexcError("Configured real OHLC series is incomplete; chart was not rendered with substitute prices")
                candles = [replace(c, open=float(c.real_open), high=float(c.real_high),
                    low=float(c.real_low), close=float(c.real_close)) for c in candles]
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = self.settings.reports_dir / f"chart_{symbol}_{stamp}.png"
            render_5m_chart(candles, path, title_suffix="visualization-only")
            return CommandResult(f"CHART — {symbol} 5M\nVisualization only; 5M data and indicators are isolated from strategy decisions.\nLatest candle: {datetime.fromtimestamp(candles[-1].close_time, timezone.utc).isoformat()}", [(str(path), "image")], "CHART_RENDERED")
        if name == "ANALYZE":
            symbol = args["symbol"]
            data = await self.market_data.fetch_symbol(symbol, window_days=15, daily_warmup_days=400, extra_intraday_warmup_days=45)
            report = analyze_series(symbol, data.timeframes, data_warnings=data.warnings)
            path = self.settings.reports_dir / f"analysis_{symbol}_{int(time.time())}.json"
            path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
            return CommandResult(format_analysis(report), [(str(path), "document")], "ANALYSIS_COMPLETED")
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
            async with self._backtest_lock:
                return await self._run_backtest(args)
        if name == "ALERT_CREATE":
            alert_id = self.alerts.create(args["symbol"], args)
            self.store.append_event("ALERT_CREATED", {"alert_id": alert_id, "args": args}, symbol=args["symbol"], idempotency_key=f"alert-created:{alert_id}")
            return CommandResult(f"ALERT CREATED — {alert_id}\n{args['symbol']} {args['kind']}\nThe alert is stored persistently. Notification delivery remains subject to the Meta policy gate.", event_type="ALERT_CREATED")
        if name == "ALERTS":
            alerts = self.alerts.list()
            if not alerts:
                return CommandResult("No alerts configured.")
            lines = [f"{a['id']} | {a['symbol']} | {a['kind']} | {'ACTIVE' if a['active'] else 'PAUSED'} | {canonical_json(a['params'])}" for a in alerts]
            return CommandResult("ALERTS\n" + "\n".join(lines[:50]))
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
        symbols = [args["symbol"]] if args.get("symbol") else list(self.settings.default_symbols)
        windows = args["windows"]
        longest = max({"1D": 1, "3D": 3, "7D": 7, "30D": 30, "90D": 90}[w] for w in windows)
        start_time = int(time.time())
        loaded: dict[str, SymbolData] = {}
        data_load_errors: dict[str, str] = {}

        # Fetch symbols concurrently while the shared MEXC client's endpoint-family
        # throttles still bound request pressure. One failed symbol must not discard
        # valid data already obtained for the other symbols.
        async def load_one(symbol: str):
            return symbol, await self.market_data.fetch_symbol(
                symbol, window_days=longest, daily_warmup_days=400,
                extra_intraday_warmup_days=45, now_epoch=start_time,
            )

        load_results = await asyncio.gather(*(load_one(s) for s in symbols), return_exceptions=True)
        for symbol, item in zip(symbols, load_results):
            if isinstance(item, Exception):
                data_load_errors[symbol] = type(item).__name__
                self.store.append_event("BACKTEST_DATA_LOAD_FAILED", {
                    "symbol": symbol, "error_type": type(item).__name__, "window_days": longest,
                }, symbol=symbol, idempotency_key=f"backtest-load-failed:{symbol}:{start_time // 60}")
                logger.warning("Backtest market data failed for %s (%s)", symbol, type(item).__name__)
            else:
                loaded[symbol] = item[1]

        series_map = {s: d.timeframes for s, d in loaded.items()}
        # Pass an empty series for failed symbols so a total data outage creates an
        # explicit no-data report per requested period instead of an invented result.
        if not series_map:
            series_map = {s: {} for s in symbols}
        specs = {s: d.contract for s, d in loaded.items() if d.contract}
        funding: dict[str, list[dict[str, Any]]] = {}
        funding_missing_symbols: list[str] = []
        mode = args.get("mode", "fixed_payout")
        if mode == "exchange_realistic":
            funding_start = start_time - (longest + 45) * 86400
            async def load_funding(symbol: str):
                return symbol, await self.mexc.funding_history_window(symbol, start=funding_start, end=start_time)
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

        cfg = BacktestConfig(
            mode=mode, starting_balance=self.settings.default_starting_balance,
            tp_pct=self.settings.fixed_tp_pct, sl_pct=self.settings.fixed_sl_pct,
            fixed_win_payout=self.settings.fixed_win_payout, fixed_loss_payout=self.settings.fixed_loss_payout,
            risk_fraction=self.settings.risk_per_trade_fraction,
            taker_fee_rate=self.settings.exchange_taker_fee_rate,
            slippage_bps_per_side=self.settings.backtest_slippage_bps_per_side,
        )
        results: list[dict[str, Any]] = []
        for window in windows:
            result = await asyncio.to_thread(
                run_backtest, series_map, window=window, config=cfg,
                contract_specs=specs,
                funding_events=funding if mode == "exchange_realistic" else None,
                now_epoch=start_time,
            )
            data_warnings = [f"{s}: {w}" for s, d in loaded.items() for w in d.warnings]
            data_warnings += [f"{s}: MEXC market-data load failed ({err}); this symbol was not simulated."
                              for s, err in data_load_errors.items()]
            if mode == "exchange_realistic" and funding_missing_symbols:
                data_warnings += [f"{s}: funding history is missing/empty; exchange-realistic P&L is incomplete."
                                  for s in funding_missing_symbols]
            result["warnings"] = list(dict.fromkeys(result["warnings"] + data_warnings))
            result["data_load_errors"] = dict(data_load_errors)
            result["funding_missing_symbols"] = list(funding_missing_symbols) if mode == "exchange_realistic" else []

            # Report cost robustness beside—not merged into—the baseline result.
            # Fixed-payout scenarios have no exchange-cost model and are not cost-stressed.
            sensitivity: list[dict[str, Any]] = []
            if mode == "exchange_realistic":
                multipliers = sorted(set((1.0, *self.settings.cost_stress_multipliers)))
                for multiplier in multipliers:
                    stress_cfg = replace(cfg, cost_multiplier=multiplier)
                    stress_result = result if multiplier == 1.0 else await asyncio.to_thread(
                        run_backtest, series_map, window=window, config=stress_cfg,
                        contract_specs=specs, funding_events=funding, now_epoch=start_time,
                    )
                    sensitivity.append({
                        "cost_multiplier": multiplier,
                        "completed_trades": stress_result["completed_trades"],
                        "wins": stress_result["wins"],
                        "losses": stress_result["losses"],
                        "win_rate_pct": stress_result["win_rate_pct"],
                        "total_pnl": stress_result["total_pnl"],
                        "ending_balance": stress_result["ending_balance"],
                        "expectancy_per_trade": stress_result["expectancy_per_trade"],
                        "expectancy_r_per_trade": stress_result["expectancy_r_per_trade"],
                        "profit_factor": stress_result["profit_factor"],
                        "max_drawdown_pct": stress_result["max_drawdown_pct"],
                        "fees": stress_result["fees"],
                        "funding_pnl": stress_result["funding_pnl"],
                        "slippage_cost": stress_result["slippage_cost"],
                    })
            result["cost_sensitivity"] = sensitivity

            run_id = self.store.create_research_run(mode, {
                "symbols": symbols, "window": window, "strategy_version": self.settings.strategy_version,
                "config": {"mode": cfg.mode, "tp_pct": cfg.tp_pct, "sl_pct": cfg.sl_pct,
                           "risk_fraction": cfg.risk_fraction, "taker_fee_rate": cfg.taker_fee_rate,
                           "slippage_bps_per_side": cfg.slippage_bps_per_side,
                           "cost_stress_multipliers": list(self.settings.cost_stress_multipliers)},
            })
            self.store.finish_research_run(run_id, result)
            results.append(result)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.settings.reports_dir / f"backtest_{mode}_{stamp}.json"
        path.write_text(json.dumps({"mode": mode, "symbols": symbols, "results": results}, indent=2, sort_keys=True), encoding="utf-8")
        lines = [f"BACKTEST COMPLETE — {mode.upper()}", f"Symbols requested: {', '.join(symbols)}",
                 f"Symbols loaded: {', '.join(loaded) if loaded else 'none'}",
                 "Each window starts with its own $100 balance."]
        if data_load_errors:
            lines.append("DATA WARNINGS: " + "; ".join(f"{s}={err}" for s, err in data_load_errors.items()))
        for r in results:
            wr = "n/a" if r["win_rate_pct"] is None else f"{r['win_rate_pct']:.2f}%"
            lines.append(f"{r['window']}: trades={r['completed_trades']} W/L={r['wins']}/{r['losses']} win-rate={wr} PnL=${r['total_pnl']:.2f} ending=${r['ending_balance']:.2f} maxDD={r['max_drawdown_pct']:.2f}%")
            if r.get("cost_sensitivity"):
                stress_line = ", ".join(f"{x['cost_multiplier']:.1f}x=${x['total_pnl']:.2f}" for x in r["cost_sensitivity"])
                lines.append(f"  Cost stress net PnL: {stress_line}")
        lines.append("A no-data or insufficient-history result is not a strategy result. Fixed payout is hypothetical; exchange-realistic P&L is modeled, not live execution. See attached JSON for warnings.")
        return CommandResult("\n".join(lines), [(str(path), "document")], "BACKTEST_COMPLETED")


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
