from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class WhatsAppConfigurationError(RuntimeError):
    """WhatsApp Cloud API is disabled or incompletely configured."""


class WhatsAppAPIError(RuntimeError):
    """A non-retryable or exhausted WhatsApp Cloud API request."""


@dataclass(frozen=True)
class WhatsAppStatus:
    enabled: bool
    configured: bool
    webhook_configured: bool
    api_version: str
    reason: str


class WhatsAppCloudAPI:
    """Small, synchronous WhatsApp Cloud API adapter with bounded retries.

    It deliberately never logs access tokens or request authorization headers.
    An injectable HTTP client keeps the sender straightforward to test offline.
    """

    def __init__(self, settings=None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    @property
    def is_enabled(self) -> bool:
        return bool(
            self.settings.whatsapp_enabled
            and self.settings.whatsapp_access_token
            and self.settings.whatsapp_phone_number_id
        )

    def status(self) -> WhatsAppStatus:
        configured = bool(self.settings.whatsapp_access_token and self.settings.whatsapp_phone_number_id)
        webhook_configured = bool(self.settings.whatsapp_verify_token and self.settings.whatsapp_app_secret)
        if not self.settings.whatsapp_enabled:
            reason = "WHATSAPP_ENABLED is false"
        elif not configured:
            reason = "Missing WhatsApp access token or phone-number ID"
        elif not webhook_configured:
            reason = "Outbound API configured; webhook verify token/app secret still required"
        else:
            reason = "Cloud API and webhook credentials configured"
        return WhatsAppStatus(
            enabled=self.is_enabled,
            configured=configured,
            webhook_configured=webhook_configured,
            api_version=self.settings.whatsapp_graph_api_version,
            reason=reason,
        )

    def _url(self) -> str:
        version = str(self.settings.whatsapp_graph_api_version or "").strip()
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
            raise WhatsAppConfigurationError("WHATSAPP_GRAPH_API_VERSION must look like v26.0")
        phone_id = str(self.settings.whatsapp_phone_number_id or "").strip()
        if not phone_id.isdigit():
            raise WhatsAppConfigurationError("WHATSAPP_PHONE_NUMBER_ID must be a numeric phone-number ID")
        return f"https://graph.facebook.com/{version}/{phone_id}/messages"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(self.settings.whatsapp_api_timeout_seconds))
        return self._client

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_enabled:
            raise WhatsAppConfigurationError(
                "WhatsApp sender is disabled or credentials are missing; check WHATSAPP_ENABLED, "
                "WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID."
            )
        headers = {
            "Authorization": f"Bearer {self.settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }
        url = self._url()
        attempts = int(self.settings.whatsapp_api_max_retries) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._http().post(url, json=payload, headers=headers)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = WhatsAppAPIError(f"Retryable WhatsApp API HTTP {response.status_code}")
                    if attempt + 1 < attempts:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = min(max(float(retry_after), 0.2), 3.0) if retry_after else min(0.4 * (2 ** attempt), 2.0)
                        except ValueError:
                            delay = min(0.4 * (2 ** attempt), 2.0)
                        time.sleep(delay)
                        continue
                if response.status_code >= 400:
                    try:
                        body = response.json()
                        error = body.get("error", body)
                        detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                    except ValueError:
                        detail = response.text[:500]
                    raise WhatsAppAPIError(f"WhatsApp API HTTP {response.status_code}: {detail[:500]}")
                try:
                    body = response.json()
                except ValueError as exc:
                    raise WhatsAppAPIError("WhatsApp API returned non-JSON success response") from exc
                if not isinstance(body, dict):
                    raise WhatsAppAPIError("WhatsApp API returned an unexpected response")
                return body
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                time.sleep(min(0.4 * (2 ** attempt), 2.0))
        raise WhatsAppAPIError(f"WhatsApp API request failed after {attempts} attempt(s): {last_error}") from last_error

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        digits = re.sub(r"[^0-9]", "", str(phone or ""))
        if not 8 <= len(digits) <= 15:
            raise ValueError("Recipient phone must contain 8 to 15 digits in international format")
        return digits

    def send_text(self, recipient_phone: str, message: str) -> dict[str, Any]:
        recipient = self._normalize_phone(recipient_phone)
        text = str(message or "").strip()
        if not text:
            raise ValueError("WhatsApp message cannot be empty")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": text[:4096]},
        }
        return self._post(payload)

    def send_image(self, recipient_phone: str, image_url: str, caption: str = "") -> dict[str, Any]:
        recipient = self._normalize_phone(recipient_phone)
        url = str(image_url or "").strip()
        if not url.startswith("https://"):
            raise ValueError("WhatsApp chart image must use a publicly reachable HTTPS URL")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "image",
            "image": {"link": url, "caption": str(caption or "")[:1024]},
        }
        return self._post(payload)

    def send_crypto_signal(self, signal: dict, recipient_phone: str | None = None) -> dict[str, Any]:
        """Send a research/paper signal summary; this does not place an exchange order."""
        recipient = recipient_phone or self.settings.whatsapp_default_recipient
        symbol = str(signal.get("symbol") or "UNKNOWN")
        side = str(signal.get("side") or signal.get("decision") or "RESEARCH")
        lines = [
            f"KRONOS PAPER RESEARCH — {symbol}",
            f"Decision: {side}",
            f"Status: {signal.get('status') or signal.get('decision') or 'RESEARCH_ONLY'}",
        ]
        for key, label in (("entry", "Entry"), ("stop", "Stop"), ("tp1", "TP1"), ("tp2", "TP2"), ("score", "Score")):
            if signal.get(key) is not None:
                lines.append(f"{label}: {signal[key]}")
        lines.append("Research only. No order was placed.")
        return self.send_text(recipient, "\n".join(lines))

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


# Backwards-compatible import name used by the status route and prior callers.
WhatsAppSignalAdapter = WhatsAppCloudAPI
WhatsAppPolicyBlocked = WhatsAppConfigurationError


def whatsapp_status_dict() -> dict[str, Any]:
    return asdict(WhatsAppCloudAPI().status())
