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
    d = signal.analysis
    score = int(d.get("score", 0) or 0)
    target_tf = d.get("target_timeframe") or "HTF"
    return (
        "🚨 MEXC FUTURES TRADE SIGNAL\n\n"
        f"{signal.symbol} — {signal.side}\n\n"
        f"📍 Entry: ${fmt_price(signal.plan.entry)}\n"
        f"🛑 SL: ${fmt_price(signal.plan.stop_loss)}\n"
        f"🎯 TP: ${fmt_price(signal.plan.tp)} ({target_tf})\n"
        f"📊 RR: 1:{signal.plan.rr:.2f}\n\n"
        f"1D: {d.get('regime_1d', 'N/A')}\n"
        f"12H: {d.get('bias_12h', 'N/A')}\n"
        f"4H: {d.get('structure_4h', 'N/A')}\n"
        f"1H: {d.get('trigger_type', 'N/A')}\n"
        f"RSI: {float(d.get('rsi', 0) or 0):.1f}\n"
        f"RVOL: {float(d.get('rvol_1h', 0) or 0):.2f}x\n"
        f"Funding: {float(d.get('mexc_funding_rate', 0) or 0):.5f}\n"
        f"📈 Score: {score}/100\n\n"
        "⚠️ Deterministic educational signal. Manage risk."
    )


class SignalManager:
    """Persist and dispatch each 4H structural setup at most once."""

    def __init__(self, db: Database, whatsapp: WhatsAppClient, recipients: set[str], expiry_minutes: int, **legacy_kwargs) -> None:
        # Backward-compatible startup guard for deployments that briefly retain an older caller.
        # Telegram is intentionally ignored and is not used by this class.
        if "telegram" in legacy_kwargs:
            LOGGER.warning("Ignoring obsolete Telegram SignalManager argument")
        self.db = db
        self.whatsapp = whatsapp
        self.recipients = set(recipients)
        self.expiry_minutes = max(1, int(expiry_minutes))

    async def publish(self, signal: ValidatedSignal) -> bool:
        existing = self.db.get_signal(signal.key)
        retry_existing = bool(existing and str(existing.status).upper() in {"SEND_FAILED", "NO_RECIPIENT"})
        if existing and not retry_existing:
            LOGGER.info("Duplicate setup blocked | symbol=%s side=%s key=%s", signal.symbol, signal.side, signal.key)
            return False

        if not self.recipients:
            LOGGER.error("Signal %s has no WhatsApp recipients configured", signal.key)
            if not existing:
                self._persist(signal, "NO_RECIPIENT")
            else:
                self.db.update_signal_status(signal.key, "NO_RECIPIENT")
            return False

        if not existing:
            self._persist(signal, "NEW")

        body = format_signal(signal)
        sent = 0
        for recipient in sorted(self.recipients):
            try:
                response = await self.whatsapp.send_text(recipient, body)
                message_id = None
                try:
                    message_id = ((response.get("messages") or [{}])[0] or {}).get("id")
                except AttributeError:
                    pass
                LOGGER.info("WhatsApp signal accepted | key=%s to=%s message_id=%s", signal.key, recipient, message_id)
                sent += 1
            except Exception:
                LOGGER.exception("WhatsApp signal dispatch failed | key=%s to=%s", signal.key, recipient)

        self.db.update_signal_status(signal.key, "SENT" if sent else "SEND_FAILED")
        return sent > 0

    def _persist(self, signal: ValidatedSignal, status: str) -> None:
        snapshot = json.dumps(signal.analysis, separators=(",", ":"), sort_keys=True, default=str)
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        expires = datetime.fromtimestamp(now.timestamp() + self.expiry_minutes * 60, tz=timezone.utc).isoformat()
        inserted = self.db.create_signal_if_new(
            signal_key=signal.key,
            symbol=signal.symbol,
            side=signal.side,
            candle_time=signal.candle_time,
            entry=signal.plan.entry,
            stop_loss=signal.plan.stop_loss,
            tp1=signal.plan.tp,
            tp2=signal.plan.tp,
            rr=signal.plan.rr,
            confluence=int(signal.analysis.get("score", 0) or 0),
            analysis_json=snapshot,
            created_at=created_at,
            expires_at=expires,
        )
        if not inserted:
            LOGGER.info("Signal persistence race/duplicate | key=%s", signal.key)
