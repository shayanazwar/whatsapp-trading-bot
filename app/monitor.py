from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.config import Settings
from app.database import Database
from app.mexc import MexcClient
from app.safety import AbnormalMoveGate
from app.strategy import StrategyConfig, evaluate
from app.whatsapp import WhatsAppClient

log = logging.getLogger(__name__)


class MarketMonitor:
    def __init__(self, settings: Settings, db: Database, mexc: MexcClient, whatsapp: WhatsAppClient):
        self.settings = settings
        self.db = db
        self.mexc = mexc
        self.whatsapp = whatsapp
        self.gate = AbnormalMoveGate(settings, db)
        self._bootstrapped = False

    async def cycle(self) -> None:
        now = int(time.time())
        candles = await self.mexc.get_recent_closed_candles(self.settings.warmup_candles, now)
        if len(candles) < self.settings.warmup_candles:
            log.warning("Monitor skipped: warm-up candle count insufficient")
            return
        latest = candles[-1]
        last_processed = int(self.db.get("monitor_last_candle", "0") or 0)
        signal = evaluate(candles, StrategyConfig(
            self.settings.ema_fast, self.settings.ema_mid, self.settings.ema_slow,
            self.settings.live_rsi_period, self.settings.live_rsi_upper,
            self.settings.live_rsi_lower, self.settings.live_tp_move_pct,
        ))
        condition = signal.direction if signal else "NONE"

        # On startup/restart, synchronize state but intentionally skip the last closed candle.
        if not self._bootstrapped:
            self.db.set("monitor_last_candle", latest.time, now)
            self.db.set("monitor_last_condition", condition, now)
            self._bootstrapped = True
            log.info("Monitor bootstrapped; historical/stale setup not alerted")
            return
        if latest.time <= last_processed:
            return

        paused, reason = self.gate.update(candles, now)
        previous_condition = self.db.get("monitor_last_condition", "NONE") or "NONE"
        self.db.set("monitor_last_candle", latest.time, now)
        self.db.set("monitor_last_condition", condition, now)
        if not signal or previous_condition == condition:
            return
        if paused:
            log.info("Signal suppressed by abnormal-movement gate: %s", reason)
            return

        # Use the live ticker only to reject stale alerts; it is never used to generate the signal.
        try:
            current_price, ticker_time = await self.mexc.get_ticker()
        except Exception as exc:
            log.warning("Signal suppressed: current ticker unavailable (%s)", type(exc).__name__)
            return
        ticker_age = now - ticker_time
        if ticker_age < -5 or ticker_age > 120:
            log.warning("Signal suppressed: ticker timestamp is stale or in the future")
            return
        deviation = abs(current_price / signal.price - 1.0) * 100.0
        if deviation > self.settings.signal_expiry_deviation_pct:
            log.info("Stale signal suppressed: price deviation %.4f%%", deviation)
            return

        candle_time = datetime.fromtimestamp(signal.candle_time, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ema_text = "Broken UP" if signal.direction == "LONG" else "Broken DOWN"
        message = (
            "BTCUSDT SIGNAL\n\n"
            f"Price: {signal.price:.1f}\n"
            f"EMAs: {ema_text}\n"
            f"RSI: {signal.rsi:.2f}\n"
            "SL: NONE\n"
            f"TP: {signal.tp_price:.1f} (+{signal.tp_pct:.2f}% from signal close; indicative)\n"
            f"Direction: {signal.direction}\n"
            "Timeframe: 1M\n"
            f"Candle: {candle_time}\n"
            "ALERT ONLY — NO ORDER PLACED"
        )
        recipients = self.settings.wa_recipients
        if not recipients:
            log.warning("Valid signal not sent: WA_RECIPIENTS is empty")
            return
        signal_key = f"{signal.candle_time}:{signal.direction}:{signal.rsi_period}:{signal.rsi_upper:g}:{signal.rsi_lower:g}"
        if not self.db.mark_signal_sent(signal_key, signal.direction, signal.candle_time, now):
            return
        # At-most-once delivery: ambiguous network failures are not blindly retried.
        for recipient in recipients:
            try:
                await self.whatsapp.send_chunks(recipient, message)
            except Exception as exc:
                log.error("Signal send failed for configured recipient (%s)", type(exc).__name__)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Market monitor cycle failed: %s", type(exc).__name__)
            # Align to just after the next UTC minute close. Bound the delay if config differs.
            now = time.time()
            delay = 60.0 - (now % 60.0) + 2.5
            await asyncio.sleep(max(10.0, delay))
