# WhatsApp integration operating note

This repository includes an optional WhatsApp Cloud API command interface for a private research/paper-trading application. Code implementation does not determine whether a particular WhatsApp Business account, message purpose, recipient, or jurisdiction is eligible for platform use.

Before enabling the integration, confirm the current requirements that apply to your own account and use case. Do not use unofficial WhatsApp Web automation or attempt to evade account or platform enforcement.

The application enforces webhook HMAC validation and a recipient allowlist. All MEXC functionality remains read-only/research-only: no exchange orders are placed.


## Message boundary in this build

WhatsApp responses are limited to command acknowledgements and operational job status. Detailed crypto-futures research, directional recommendations, entries, stops, targets and signal lists are not sent through WhatsApp. The authenticated dashboard remains the research feed. This implementation boundary does not decide whether a particular account or use case is permitted by the platform.
