from __future__ import annotations

from dataclasses import dataclass


class WhatsAppPolicyBlocked(PermissionError):
    pass


@dataclass(frozen=True)
class WhatsAppStatus:
    enabled: bool
    policy: str
    reason: str


class WhatsAppSignalAdapter:
    """Hard block: Meta's Business Messaging Policy restricts promoting/facilitating virtual currency exchange.

    This adapter deliberately does not send crypto-futures trading signals, even if environment
    variables are set. A wording change or local licence does not bypass Meta's published restriction.
    """
    def status(self) -> WhatsAppStatus:
        return WhatsAppStatus(
            enabled=False,
            policy="BLOCKED_FOR_CRYPTO_TRADING_SIGNALS",
            reason=("Meta Business Messaging Policy restricts buying, selling, promoting or otherwise "
                    "facilitating exchange of real or virtual currency. Use the authenticated dashboard "
                    "and its in-app signal feed for this application."),
        )

    def send_crypto_signal(self, *_args, **_kwargs):
        raise WhatsAppPolicyBlocked(
            "WhatsApp delivery of cryptocurrency futures signals is blocked by Meta Business Messaging Policy."
        )
