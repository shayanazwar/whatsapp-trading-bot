from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..database import Database
from ..formatting import fmt_price
from ..whatsapp import WhatsAppClient
from .signal_validator import ValidatedSignal

LOGGER = logging.getLogger(__name__)


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def format_signal(signal: ValidatedSignal) -> str:
    d = signal.analysis
    score = int(d.get("score", 0) or 0)
    families = int(d.get("confirmation_families_passed", d.get("confirmation_family_count", 0)) or 0)
    available = int(d.get("confirmation_families_available", 8) or 8)
    funding = d.get("mexc_funding_rate")
    funding_text = "N/A" if funding is None else f"{float(funding):.5f}"
    target_tf = d.get("target_timeframe") or "HTF"
    return (
        "🚨 MEXC FUTURES TRADE SIGNAL\n\n"
        f"{signal.symbol} — {signal.side}\n\n"
        f"📍 Entry: ${fmt_price(signal.plan.entry)}\n"
        f"🛑 SL: ${fmt_price(signal.plan.stop_loss)}\n"
        f"🎯 TP: ${fmt_price(signal.plan.tp)} ({target_tf})\n"
        f"📊 RR: 1:{signal.plan.rr:.2f}\n\n"
        f"4H: {d.get('trend_4h', 'N/A')}\n"
        f"1H: {d.get('structure_1h', 'N/A')}\n"
        f"15M: {'BOS + RETEST' if d.get('bos_15m') and d.get('setup_retest_time') else 'N/A'}\n"
        "5M: OPTIONAL\n"
        f"RSI: {float(d.get('rsi', 0) or 0):.1f}\n"
        f"RVOL: {float(d.get('rvol_15m', 0) or 0):.2f}x\n"
        f"Funding: {funding_text}\n"
        f"📈 Score: {score}/100\n"
        f"✅ Families: {families}/{available}\n\n"
        "⚠️ Deterministic educational signal. Manage risk."
    )


class SignalManager:
    """Publish each structural setup at most once.

    The identity is provided by signal_validator.make_signal_key(): symbol +
    side + BOS timestamp + BOS level. Scanner cadence and small entry changes
    therefore cannot create duplicate messages for the same setup.
    """

    def __init__(
        self,
        db: Database,
        whatsapp: WhatsAppClient,
        recipients: set[str],
        expiry_minutes: int,
    ) -> None:
        self.db = db
        self.whatsapp = whatsapp
        self.recipients = set(recipients)
        self.expiry_minutes = max(1, int(expiry_minutes))

    async def publish(self, signal: ValidatedSignal) -> bool:
        last = self.db.get_last_signal_for_symbol_side(signal.symbol, signal.side)
        if last:
            try:
                previous = json.loads(last.analysis_json)
            except Exception:
                previous = {}

            old_key = str(last.signal_key or "")
            if old_key == signal.key:
                LOGGER.info(
                    "Duplicate structural setup blocked %s %s key=%s",
                    signal.symbol,
                    signal.side,
                    signal.key,
                )
                return False

            # Compatibility path for older records created before structural
            # identity existed. New records are governed by the unique key.
            old_bos = previous.get("setup_bos_time")
            new_bos = signal.analysis.get("setup_bos_time")
            try:
                if old_bos and new_bos and int(old_bos) == int(new_bos):
                    LOGGER.info(
                        "Duplicate BOS setup blocked %s %s bos=%s",
                        signal.symbol,
                        signal.side,
                        new_bos,
                    )
                    return False
            except (TypeError, ValueError):
                pass

        snapshot = json.dumps(
            signal.analysis,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        expires = datetime.fromtimestamp(
            now.timestamp() + self.expiry_minutes * 60,
            tz=timezone.utc,
        ).isoformat()

        # The database schema is retained for backward compatibility. Both
        # legacy columns carry the same single TP; no partial-target state exists.
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
            return False

        if not self.recipients:
            self.db.update_signal_status(signal.key, "NO_RECIPIENT")
            return False

        body = format_signal(signal)
        successful = 0
        for recipient in sorted(self.recipients):
            try:
                await self.whatsapp.send_text(recipient, body)
                successful += 1
            except Exception:
                LOGGER.exception(
                    "Failed to send signal %s to %s",
                    signal.key,
                    recipient,
                )

        self.db.update_signal_status(
            signal.key,
            "SENT" if successful else "SEND_FAILED",
        )
        return successful > 0
