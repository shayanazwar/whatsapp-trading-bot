from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from rslr.charts import generate_chart
from rslr.config import settings
from rslr.data.candles import build_timeframes
from rslr.exchange.mexc import MEXCRestClient
from rslr.models import TradeSignal
from rslr.intelligence import relative_strength
from rslr.storage import Database
from rslr.strategy import SignalEngine

log = logging.getLogger(__name__)


def scan_market(client: MEXCRestClient | None = None, database: Database | None = None) -> list[TradeSignal]:
    owned = client is None
    client = client or MEXCRestClient()
    database = database or Database()
    try:
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=42)
        contracts = client.get_universe(settings.universe_size)
        raw_contracts = client.get_contracts()
        all_contracts = {c.symbol: c for c in raw_contracts}
        database.save_market_metadata(raw_contracts, [c.symbol for c in contracts])
        try:
            database.save_oi_snapshots(client.get_tickers())
        except Exception:
            log.exception("Current OI snapshot collection failed; OI remains optional")
        btc_contract = all_contracts.get("BTC_USDT")
        if btc_contract is None:
            raise RuntimeError("BTC_USDT contract metadata unavailable")
        btc = client.get_klines("BTC_USDT", start, end, "Min60")
        if len(btc) < 960:
            raise RuntimeError(f"Not enough BTC warm-up data ({len(btc)} hourly candles)")
        btc_frames = build_timeframes(btc)
        asof = pd.Timestamp(btc.iloc[-1].timestamp) + pd.Timedelta(hours=1)
        btc_12h = btc_frames["12H"]
        btc_12h = btc_12h[btc_12h.timestamp + pd.Timedelta(hours=12) <= asof]
        engine = SignalEngine()
        candidates: list[TradeSignal] = []
        relative_scores: dict[str, float] = {}
        known = {c.symbol: c for c in contracts}
        known.setdefault("BTC_USDT", btc_contract)
        for symbol, contract in known.items():
            try:
                frame = btc if symbol == "BTC_USDT" else client.get_klines(symbol, start, end, "Min60")
                if len(frame) < 960:
                    continue
                funding = client.get_funding_history(symbol, start, end)
                if funding.empty:
                    continue
                symbol_frames = build_timeframes(frame)
                symbol_12h = symbol_frames["12H"]
                symbol_12h = symbol_12h[symbol_12h.timestamp + pd.Timedelta(hours=12) <= asof]
                score = relative_strength(symbol_12h, btc_12h)
                if score is not None:
                    relative_scores[symbol] = score
                database.save_candles(symbol, "1H", frame.tail(2))
                for tf_name in ("4H", "12H", "1D"):
                    if not symbol_frames[tf_name].empty:
                        database.save_candles(symbol, tf_name, symbol_frames[tf_name].tail(1))
                database.save_funding(symbol, funding.tail(3))
                signals = engine.generate_for_symbol(
                    symbol=symbol,
                    symbol_1h=frame,
                    btc_1h=btc,
                    funding=funding,
                    contract=contract,
                    equity=settings.account_equity_usd,
                    symbol_frames_cache=symbol_frames,
                    btc_frames_cache=btc_frames,
                )
                candidates.extend(signals)
            except Exception:
                log.exception("Scan failed for %s", symbol)
        # Cross-sectional ranking across the full currently eligible universe.
        # Small samples skip the percentile rule and still require positive/negative BTC-relative strength.
        if len(relative_scores) >= 5:
            ranked = sorted(relative_scores.items(), key=lambda item: item[1], reverse=True)
            keep_n = max(1, int(len(ranked) * 0.35 + 0.999999))
            long_symbols = {symbol for symbol, _ in ranked[:keep_n]}
            short_symbols = {symbol for symbol, _ in ranked[-keep_n:]}
            candidates = [
                signal for signal in candidates
                if (signal.side == "LONG" and signal.symbol in long_symbols)
                or (signal.side == "SHORT" and signal.symbol in short_symbols)
            ]
            rank_index = {symbol: index + 1 for index, (symbol, _) in enumerate(ranked)}
            for signal in candidates:
                signal.metadata["relative_strength_rank"] = rank_index.get(signal.symbol)
                signal.metadata["relative_strength_universe_size"] = len(ranked)
        candidates.sort(key=lambda s: abs(s.relative_strength_score), reverse=True)

        recent = database.latest_signals(100)
        now = datetime.now(timezone.utc)
        signals_24h = [
            x for x in recent
            if now - datetime.fromisoformat(x["signal_time"]).astimezone(timezone.utc) < timedelta(hours=24)
        ]
        cooldowns: dict[tuple[str, str], datetime] = {}
        for item in recent:
            key = (item["symbol"], item["side"])
            stamp = datetime.fromisoformat(item["signal_time"]).astimezone(timezone.utc)
            cooldowns[key] = max(cooldowns.get(key, stamp), stamp)

        accepted: list[TradeSignal] = []
        seen: set[tuple[str, str, str]] = set()
        for signal in candidates:
            key = (signal.symbol, signal.side, signal.signal_time.isoformat())
            if key in seen:
                continue
            seen.add(key)
            recent_stamp = cooldowns.get((signal.symbol, signal.side))
            if recent_stamp and signal.signal_time - recent_stamp < timedelta(hours=settings.same_symbol_cooldown_hours):
                continue
            if len(signals_24h) + len(accepted) >= settings.max_signals_per_24h:
                break
            if len(accepted) >= settings.max_signals_per_scan:
                break
            payload = signal.to_dict()
            if database.save_signal(payload):
                accepted.append(signal)
        return accepted
    finally:
        if owned:
            client.close()


def create_market_chart(
    symbol: str, timeframe: str = "1H", client: MEXCRestClient | None = None, database: Database | None = None
):
    timeframe = timeframe.upper()
    if timeframe not in {"1H", "4H", "12H", "1D"}:
        raise ValueError("Supported timeframes: 1H, 4H, 12H, 1D")
    owned = client is None
    client = client or MEXCRestClient()
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=60)
        hourly = client.get_klines(symbol, start, end, "Min60")
        if hourly.empty:
            raise RuntimeError(f"No completed MEXC candles returned for {symbol}")
        frame = build_timeframes(hourly)[timeframe] if timeframe != "1H" else hourly
        database = database or Database()
        recent = [x for x in database.latest_signals(30) if x.get("symbol") == symbol]
        signal = recent[0] if recent else None
        return generate_chart(frame, symbol, timeframe, signal=signal)
    finally:
        if owned:
            client.close()
