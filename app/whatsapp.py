    async def send_service_status(self, recipient: str) -> dict[str, Any]:
        """Send one fixed, non-trading service notice to an allowlisted sender.

        This deliberately does not enable crypto/trading responses.
        """
        recipient_key = sender_key(recipient)
        allowed = {
            sender_key(value)
            for value in self.settings.whatsapp_allowed_senders
        }
        allowed.discard("")

        if not recipient_key or recipient_key not in allowed:
            raise WhatsAppError(
                "Service-status recipient is not in the allowlist"
            )

        if not self.settings.meta_access_token:
            raise WhatsAppError("META_ACCESS_TOKEN is not configured")

        # Fixed generic service message only; never include market analysis,
        # trading signals, prices, or backtest results in this path.
        text = (
            "Thanks for your message. Automated responses are temporarily "
            "unavailable. Please try again later."
        )

        client = await self._ensure_client()
        response = await client.post(
            self._endpoint(),
            headers={
                "Authorization": (
                    f"Bearer {self.settings.meta_access_token}"
                )
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_key,
                "type": "text",
                "text": {
                    "preview_url": False,
                    "body": text,
                },
            },
        )

        if response.is_error:
            raise WhatsAppError(
                "Meta service-status message failed with HTTP "
                f"{response.status_code}"
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise WhatsAppError(
                "Meta returned invalid JSON for service-status message"
            ) from exc

        return result if isinstance(result, dict) else {}
