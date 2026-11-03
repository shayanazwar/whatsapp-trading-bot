from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class WhatsAppConfigurationError(RuntimeError):
    """Raised when WhatsApp Cloud API credentials or recipient details are missing."""


class WhatsAppAPIError(RuntimeError):
    """Raised when WhatsApp Cloud API rejects a request or remains unavailable."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class WhatsAppPolicyBlocked(PermissionError):
    """Legacy exception retained for callers importing the previous adapter API."""


@dataclass(frozen=True)
class WhatsAppStatus:
    enabled: bool
    policy: str
    reason: str


class WhatsAppSignalAdapter:
    """WhatsApp Cloud API text sender, configured exclusively through environment variables.

    Supported credentials (either naming convention):
      - WHATSAPP_ACCESS_TOKEN / META_ACCESS_TOKEN
      - WHATSAPP_PHONE_NUMBER_ID / META_PHONE_NUMBER_ID

    Optional settings:
      - WHATSAPP_ENABLED=true|false (defaults to true when credentials exist)
      - WHATSAPP_API_URL=https://graph.facebook.com/v26.0
      - WHATSAPP_API_VERSION=v26.0 (used only when WHATSAPP_API_URL is absent)
      - WHATSAPP_DEFAULT_RECIPIENT=923001234567
      - WHATSAPP_TIMEOUT_SECONDS=15
      - WHATSAPP_MAX_ATTEMPTS=3 (1..5)
      - WHATSAPP_RETRY_BASE_SECONDS=0.5

    A successful return means the API accepted the message and returned a message ID; it
    does not assert that the recipient has read the message.
    """

    RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
    MAX_MESSAGE_LENGTH = 4096

    def __init__(self, environ: Mapping[str, str] | None = None, client_factory=None):
        env = os.environ if environ is None else environ
        self.access_token = self._first_env(env, "WHATSAPP_ACCESS_TOKEN", "META_ACCESS_TOKEN")
        self.phone_number_id = self._first_env(
            env, "WHATSAPP_PHONE_NUMBER_ID", "META_PHONE_NUMBER_ID"
        )
        self.default_recipient = self._first_env(
            env, "WHATSAPP_DEFAULT_RECIPIENT", "META_DEFAULT_RECIPIENT"
        )
        api_url = self._first_env(env, "WHATSAPP_API_URL")
        api_version = self._first_env(env, "WHATSAPP_API_VERSION") or "v26.0"
        self.api_url = (api_url or f"https://graph.facebook.com/{api_version}").rstrip("/")
        self.enabled_flag = self._parse_bool(self._first_env(env, "WHATSAPP_ENABLED"), default=True)
        self.timeout_seconds = self._positive_float(env.get("WHATSAPP_TIMEOUT_SECONDS", "15"), 15.0)
        self.max_attempts = self._bounded_int(env.get("WHATSAPP_MAX_ATTEMPTS", "3"), 3, 1, 5)
        self.retry_base_seconds = self._positive_float(
            env.get("WHATSAPP_RETRY_BASE_SECONDS", "0.5"), 0.5
        )
        self._client_factory = client_factory or httpx.AsyncClient

    @staticmethod
    def _first_env(env: Mapping[str, str], *names: str) -> str:
        for name in names:
            value = str(env.get(name, "")).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _parse_bool(value: str, *, default: bool) -> bool:
        if not value:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}

    @staticmethod
    def _positive_float(value: str, fallback: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return number if 0 < number <= 120 else fallback

    @staticmethod
    def _bounded_int(value: str, fallback: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return fallback
        return min(max(number, minimum), maximum)

    @property
    def is_enabled(self) -> bool:
        return bool(self.enabled_flag and self.access_token and self.phone_number_id)

    def status(self) -> WhatsAppStatus:
        if not self.enabled_flag:
            return WhatsAppStatus(False, "DISABLED_BY_CONFIGURATION", "WHATSAPP_ENABLED is false")
        if not self.access_token or not self.phone_number_id:
            missing = []
            if not self.access_token:
                missing.append("WHATSAPP_ACCESS_TOKEN (or META_ACCESS_TOKEN)")
            if not self.phone_number_id:
                missing.append("WHATSAPP_PHONE_NUMBER_ID (or META_PHONE_NUMBER_ID)")
            return WhatsAppStatus(False, "NOT_CONFIGURED", "Missing: " + ", ".join(missing))
        return WhatsAppStatus(
            True,
            "CONFIGURED",
            "Credentials are configured. Account permissions and recipient eligibility are verified by the API on send.",
        )

    @classmethod
    def _normalize_phone(cls, recipient_phone: str) -> str:
        if not isinstance(recipient_phone, str):
            raise ValueError("Recipient phone must be a string in international format")
        normalized = re.sub(r"[\s()+.-]", "", recipient_phone)
        if not re.fullmatch(r"[1-9][0-9]{7,14}", normalized):
            raise ValueError("Recipient phone must be an international number with 8 to 15 digits")
        return normalized

    @classmethod
    def _format_signal(cls, signal: str | Mapping[str, Any] | Any) -> str:
        if isinstance(signal, str):
            message = signal.strip()
        elif isinstance(signal, Mapping):
            labels = (
                ("symbol", "Symbol"),
                ("side", "Direction"),
                ("status", "Status"),
                ("entry", "Entry"),
                ("stop", "Stop loss"),
                ("tp1", "Take profit 1"),
                ("tp2", "Take profit 2"),
                ("score", "Score"),
                ("timeframe", "Timeframe"),
                ("as_of", "Time"),
            )
            lines = ["KRONOS-MEXC-INTELLIGENCE"]
            strategy = signal.get("strategy") if isinstance(signal.get("strategy"), Mapping) else {}
            for key, label in labels:
                value = signal.get(key)
                if value is None or value == "":
                    value = strategy.get(key)
                if key == "status" and (value is None or value == ""):
                    value = signal.get("decision")
                if value is not None and value != "":
                    lines.append(f"{label}: {value}")
            reasons = signal.get("reasons") or strategy.get("reasons")
            if isinstance(reasons, (list, tuple)) and reasons:
                lines.append("Reasons:")
                lines.extend(f"- {str(reason)}" for reason in reasons[:8])
            if len(lines) == 1:
                lines.append(json.dumps(signal, ensure_ascii=False, default=str))
            message = "\n".join(lines)
        else:
            message = str(signal).strip()

        if not message:
            raise ValueError("WhatsApp message cannot be empty")
        if len(message) > cls.MAX_MESSAGE_LENGTH:
            raise ValueError(
                f"WhatsApp text is {len(message)} characters; maximum is {cls.MAX_MESSAGE_LENGTH}"
            )
        return message

    def _require_configuration(self) -> None:
        if not self.is_enabled:
            status = self.status()
            raise WhatsAppConfigurationError(
                f"WhatsApp is not ready ({status.policy}): {status.reason}"
            )
        if not self.api_url.startswith("https://"):
            raise WhatsAppConfigurationError("WhatsApp API URL must use HTTPS")
        if not re.fullmatch(r"[0-9]+", self.phone_number_id):
            raise WhatsAppConfigurationError("WhatsApp phone-number ID must contain digits only")

    @staticmethod
    def _retry_after(response: httpx.Response, fallback: float) -> float:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return min(max(float(value), 0.0), 60.0)
            except ValueError:
                pass
        return fallback

    async def send_crypto_signal(
        self,
        signal: str | Mapping[str, Any] | Any,
        recipient_phone: str | None = None,
    ) -> dict[str, Any]:
        """Send a signal message and return the Cloud API response."""
        self._require_configuration()
        recipient = recipient_phone or self.default_recipient
        if not recipient:
            raise WhatsAppConfigurationError(
                "No recipient supplied; pass recipient_phone or set WHATSAPP_DEFAULT_RECIPIENT"
            )
        recipient = self._normalize_phone(recipient)
        message = self._format_signal(signal)

        url = f"{self.api_url}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }

        last_error: Exception | None = None
        async with self._client_factory(timeout=self.timeout_seconds) as client:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    response = await client.post(url, json=payload, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc
                    if attempt >= self.max_attempts:
                        break
                    delay = min(self.retry_base_seconds * (2 ** (attempt - 1)), 10.0)
                    log.warning("WhatsApp transport error on attempt %s/%s: %s", attempt, self.max_attempts, exc)
                    await asyncio.sleep(delay)
                    continue

                if response.status_code < 400:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise WhatsAppAPIError(
                            "WhatsApp API returned a non-JSON success response",
                            status_code=response.status_code,
                        ) from exc
                    if not isinstance(data, dict):
                        raise WhatsAppAPIError(
                            "WhatsApp API returned an unexpected success payload",
                            status_code=response.status_code,
                        )
                    return data

                body = response.text[:1000]
                retryable = response.status_code in self.RETRYABLE_STATUS_CODES
                last_error = WhatsAppAPIError(
                    f"WhatsApp API error {response.status_code}: {body}",
                    status_code=response.status_code,
                    retryable=retryable,
                )
                if not retryable or attempt >= self.max_attempts:
                    raise last_error
                fallback = min(self.retry_base_seconds * (2 ** (attempt - 1)), 10.0)
                await asyncio.sleep(self._retry_after(response, fallback))

        raise WhatsAppAPIError(
            f"WhatsApp send failed after {self.max_attempts} attempts: {last_error}",
            retryable=True,
        ) from last_error
