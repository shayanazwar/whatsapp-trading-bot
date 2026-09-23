from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..database import Database
from ..formatting import fmt_price
from ..whatsapp import WhatsAppClient
from .signal_validator import ValidatedSignal

LOGGER = logging.getLogger(__name__)


def format_signal(signal: ValidatedSignal) -> str:
    data = signal.analysis
    return (
        "🚨 TRADE SIGNAL\n\n"
        "━━━━━━━━━━━━━━━━\n"
        f"{signal.symbol} — {signal.side}\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"📍 Entry\n${fmt_price(signal.plan.entry)}\n\n"
        f"🛑 Stop Loss\n${fmt_price(signal.plan.stop_loss)}\n\n"
        f"🎯 TP1\n${fmt_price(signal.plan.tp1)}\n\n"
        f"🎯 TP2\n${fmt_price(signal.plan.tp2)}\n\n"
        f"📊 Risk/Reward\n1:{signal.plan.rr:.2f}\n\n"
        f"📈 4H\n{data['trend_4h']}\n\n"
        f"📊 1H\n{data['structure_1h']}\n\n"
        f"⚡ 15M\n{data['bos_15m']}\n\n"
        f"EMA 21/50\n{data['ema_direction']}\n\n"
        f"RSI\n{data['rsi']:.1f}\n\n"
        f"Volume\n{data['volume']}\n\n"
        f"Confluence\n{data['score']}/6\n"
        "━━━━━━━━━━━━━━━━"
    )


class SignalManager:
    def __init__(self, db: Database, whatsapp: WhatsAppClient, recipients: set[str], expiry_minutes: int) -> None:
        self.db = db
        self.whatsapp = whatsapp
        self.recipients = set(recipients)
        self.expiry_minutes = max(1, expiry_minutes)

    async def publish(self, signal: ValidatedSignal) -> bool:
        snapshot = json.dumps(signal.analysis, separators=(",", ":"), sort_keys=True)
        created_at = datetime.now(timezone.utc).isoformat()
        expires_at = datetime.now(timezone.utc).timestamp() + self.expiry_minutes * 60
        expires_text = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()

        inserted = self.db.create_signal_if_new(
            signal_key=signal.key,
            symbol=signal.symbol,
            side=signal.side,
            candle_time=signal.candle_time,
            entry=signal.plan.entry,
            stop_loss=signal.plan.stop_loss,
            tp1=signal.plan.tp1,
            tp2=signal.plan.tp2,
            rr=signal.plan.rr,
            confluence=int(signal.analysis["score"]),
            analysis_json=snapshot,
            created_at=created_at,
            expires_at=expires_text,
        )
        if not inserted:
            return False

        if not self.recipients:
            LOGGER.warning("Valid signal %s created but AUTO_SIGNAL_RECIPIENTS/ALLOWED_USERS is empty", signal.key)
            self.db.update_signal_status(signal.key, "NO_RECIPIENT")
            return True

        body = format_signal(signal)
        successful = 0
        for recipient in sorted(self.recipients):
            try:
                await self.whatsapp.send_text(recipient, body)
                successful += 1
            except Exception:
                LOGGER.exception("Failed to send automatic signal %s to %s", signal.key, recipient)

        if successful:
            self.db.update_signal_status(signal.key, "SENT")
        else:
            self.db.update_signal_status(signal.key, "SEND_FAILED")
        return True
