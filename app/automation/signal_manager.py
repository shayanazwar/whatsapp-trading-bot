from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..database import Database
from ..formatting import fmt_price
from ..whatsapp import WhatsAppClient
from .signal_validator import ValidatedSignal

LOGGER = logging.getLogger(__name__)
DEFAULT_COOLDOWN_MINUTES = 60


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def format_signal(signal: ValidatedSignal) -> str:
    d = signal.analysis
    score = int(d.get("score", 0) or 0); families = int(d.get("confirmation_family_count", 0) or 0)
    funding = d.get("mexc_funding_rate")
    funding_text = "N/A" if funding is None else f"{float(funding):.5f}"
    return (
        "🚨 MEXC FUTURES TRADE SIGNAL\n\n"
        f"{signal.symbol} — {signal.side}\n\n"
        f"📍 Entry: ${fmt_price(signal.plan.entry)}\n"
        f"🛑 SL: ${fmt_price(signal.plan.stop_loss)}\n"
        f"🎯 TP1: ${fmt_price(signal.plan.tp1)}\n"
        f"🎯 TP2: ${fmt_price(signal.plan.tp2)}\n"
        f"📊 RR: 1:{signal.plan.rr:.2f}\n\n"
        f"4H: {d.get('trend_4h', 'N/A')}\n"
        f"1H: {d.get('structure_1h', 'N/A')}\n"
        f"15M: {'BOS + RETEST' if d.get('bos_15m') and d.get('setup_retest_time') else 'N/A'}\n"
        f"5M: {d.get('trigger_5m', 'N/A')}\n"
        f"RSI: {float(d.get('rsi', 0) or 0):.1f}\n"
        f"RVOL: {float(d.get('rvol_15m', 0) or 0):.2f}x / {float(d.get('rvol_5m', 0) or 0):.2f}x\n"
        f"Funding: {funding_text}\n"
        f"📈 Score: {score}/100\n"
        f"✅ Families: {families}/6\n\n"
        "⚠️ Deterministic educational signal. Manage risk."
    )


class SignalManager:
    def __init__(self, db: Database, whatsapp: WhatsAppClient, recipients: set[str], expiry_minutes: int) -> None:
        self.db = db; self.whatsapp = whatsapp; self.recipients = set(recipients); self.expiry_minutes = max(1, int(expiry_minutes))

    async def publish(self, signal: ValidatedSignal) -> bool:
        last = self.db.get_last_signal_for_symbol_side(signal.symbol, signal.side)
        if last:
            try:
                age_seconds = (datetime.now(timezone.utc) - _dt(last.created_at)).total_seconds()
            except Exception:
                age_seconds = 10**9
            try:
                previous = json.loads(last.analysis_json)
            except Exception:
                previous = {}
            new_bos = signal.analysis.get("setup_bos_time")
            old_bos = previous.get("setup_bos_time")
            structural_reset = bool(new_bos and (old_bos is None or int(new_bos) > int(old_bos)))
            if not structural_reset and age_seconds < DEFAULT_COOLDOWN_MINUTES * 60:
                LOGGER.info("Signal cooldown blocked %s %s", signal.symbol, signal.side)
                return False

        snapshot = json.dumps(signal.analysis, separators=(",", ":"), sort_keys=True, default=str)
        now = datetime.now(timezone.utc); created_at = now.isoformat(); expires = datetime.fromtimestamp(now.timestamp() + self.expiry_minutes * 60, tz=timezone.utc).isoformat()
        inserted = self.db.create_signal_if_new(signal_key=signal.key, symbol=signal.symbol, side=signal.side, candle_time=signal.candle_time, entry=signal.plan.entry, stop_loss=signal.plan.stop_loss, tp1=signal.plan.tp1, tp2=signal.plan.tp2, rr=signal.plan.rr, confluence=int(signal.analysis.get("score", 0) or 0), analysis_json=snapshot, created_at=created_at, expires_at=expires)
        if not inserted: return False
        if not self.recipients:
            self.db.update_signal_status(signal.key, "NO_RECIPIENT")
            return False
        body = format_signal(signal); successful = 0
        for recipient in sorted(self.recipients):
            try:
                await self.whatsapp.send_text(recipient, body); successful += 1
            except Exception:
                LOGGER.exception("Failed to send signal %s to %s", signal.key, recipient)
        self.db.update_signal_status(signal.key, "SENT" if successful else "SEND_FAILED")
        return successful > 0
