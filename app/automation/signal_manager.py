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

    score = int(data.get("score", 0) or 0)
    families = int(
        data.get("confirmation_family_count", 0) or 0
    )

    return (
        "🚨 MEXC FUTURES TRADE SIGNAL\n\n"
        "━━━━━━━━━━━━━━━━\n"
        f"{signal.symbol} — {signal.side}\n"
        "━━━━━━━━━━━━━━━━\n\n"

        f"📍 Entry\n"
        f"${fmt_price(signal.plan.entry)}\n\n"

        f"🛑 Stop Loss\n"
        f"${fmt_price(signal.plan.stop_loss)}\n\n"

        f"🎯 TP1\n"
        f"${fmt_price(signal.plan.tp1)}\n\n"

        f"🎯 TP2\n"
        f"${fmt_price(signal.plan.tp2)}\n\n"

        f"📊 Risk/Reward\n"
        f"1:{signal.plan.rr:.2f}\n\n"

        f"📈 4H Regime\n"
        f"{data.get('trend_4h', 'N/A')}\n\n"

        f"📊 1H Structure\n"
        f"{data.get('structure_1h', 'N/A')}\n\n"

        f"⚡ 15M Setup\n"
        f"{data.get('bos_15m', 'N/A')}\n\n"

        f"🎯 5M Trigger\n"
        f"{data.get('trigger_5m', 'N/A')}\n\n"

        f"EMA 21/50\n"
        f"{data.get('ema_direction', 'N/A')}\n\n"

        f"RSI\n"
        f"{float(data.get('rsi', 0) or 0):.1f}\n\n"

        f"RVOL 15M\n"
        f"{float(data.get('rvol_15m', 0) or 0):.2f}x\n\n"

        f"RVOL 5M\n"
        f"{float(data.get('rvol_5m', 0) or 0):.2f}x\n\n"

        f"Volume\n"
        f"{data.get('volume', 'N/A')}\n\n"

        f"📊 Score\n"
        f"{score}/100\n\n"

        f"✅ Confirmation Families\n"
        f"{families}/6\n\n"

        "━━━━━━━━━━━━━━━━\n"
        "⚠️ Educational signal — manage risk."
    )


class SignalManager:
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
        self.expiry_minutes = max(
            1,
            expiry_minutes,
        )

    async def publish(
        self,
        signal: ValidatedSignal,
    ) -> bool:
        """
        Persist and publish one validated signal.

        Database uniqueness remains the first duplicate-protection
        layer. The signal manager does not create a second signal
        if the same signal key already exists.
        """

        snapshot = json.dumps(
            signal.analysis,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )

        now = datetime.now(timezone.utc)

        created_at = now.isoformat()

        expires_at = (
            now.timestamp()
            + self.expiry_minutes * 60
        )

        expires_text = (
            datetime.fromtimestamp(
                expires_at,
                tz=timezone.utc,
            ).isoformat()
        )

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
            confluence=int(
                signal.analysis.get(
                    "score",
                    0,
                )
                or 0
            ),
            analysis_json=snapshot,
            created_at=created_at,
            expires_at=expires_text,
        )

        if not inserted:
            LOGGER.info(
                "Duplicate signal ignored: %s",
                signal.key,
            )
            return False

        # --------------------------------------------------------------
        # NO RECIPIENTS
        # --------------------------------------------------------------

        if not self.recipients:
            LOGGER.warning(
                "Valid signal %s created but "
                "AUTO_SIGNAL_RECIPIENTS/ALLOWED_USERS is empty",
                signal.key,
            )

            self.db.update_signal_status(
                signal.key,
                "NO_RECIPIENT",
            )

            return True

        # --------------------------------------------------------------
        # WHATSAPP MESSAGE
        # --------------------------------------------------------------

        body = format_signal(signal)

        successful = 0

        for recipient in sorted(
            self.recipients
        ):
            try:
                await self.whatsapp.send_text(
                    recipient,
                    body,
                )

                successful += 1

            except Exception:
                LOGGER.exception(
                    "Failed to send automatic signal "
                    "%s to %s",
                    signal.key,
                    recipient,
                )

        # --------------------------------------------------------------
        # FINAL STATUS
        # --------------------------------------------------------------

        if successful:
            self.db.update_signal_status(
                signal.key,
                "SENT",
            )

            LOGGER.info(
                "Automatic MEXC signal sent: "
                "%s %s",
                signal.symbol,
                signal.side,
            )

        else:
            self.db.update_signal_status(
                signal.key,
                "SEND_FAILED",
            )

            LOGGER.error(
                "Automatic MEXC signal %s "
                "could not be delivered",
                signal.key,
            )

        return True
