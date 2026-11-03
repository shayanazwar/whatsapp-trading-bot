import os
import httpx
import logging
from app.config import settings

logger = logging.getLogger(__name__)

class WhatsAppPolicyBlocked(Exception):
    """Legacy exception retained for backwards compatibility if needed."""
    pass

class WhatsAppAdapter:
    def __init__(self):
        self.api_url = getattr(settings, "WHATSAPP_API_URL", "https://graph.facebook.com/v17.0")
        self.token = getattr(settings, "WHATSAPP_ACCESS_TOKEN", os.getenv("WHATSAPP_ACCESS_TOKEN", ""))
        self.phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""))
        
    @property
    def is_enabled(self) -> bool:
        # WhatsApp is now active and enabled
        return True

    async def send_signal(self, recipient_phone: str, message: str) -> dict:
        """Sends a crypto signal or notification via WhatsApp Cloud API."""
        if not self.token or not self.phone_number_id:
            logger.warning("WhatsApp credentials not configured. Running in simulation/development mode.")
            return {"status": "simulated", "recipient": recipient_phone, "message": message}
            
        url = f"{self.api_url}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_phone,
            "type": "text",
            "text": {"body": message}
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                logger.error(f"Failed to send WhatsApp message: {response.text}")
                raise Exception(f"WhatsApp API error: {response.text}")
            return response.json()

whatsapp_adapter = WhatsAppAdapter()
