"""UTC logging with basic secret redaction."""
from __future__ import annotations
import logging
import os
import re
import time

_SECRET_PATTERNS = [
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~-]+"),
    re.compile(r'(?i)(access_token["\'=:\s]+)[^\s,"\']+'),
    re.compile(r'(?i)(app_secret["\'=:\s]+)[^\s,"\']+'),
    re.compile(r'(?i)(https://api\.telegram\.org/bot)[^/\s]+'),
]

class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            for pattern in _SECRET_PATTERNS:
                message = pattern.sub(r"\1[REDACTED]", message)
            record.msg = message
            record.args = ()
        except Exception:
            record.msg = "[log message redacted after formatting error]"
            record.args = ()
        return True


def configure_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        if handler.formatter is None:
            handler.setFormatter(logging.Formatter(
                "%(asctime)sZ %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            ))
        # The literal Z is only truthful when the converter is explicitly UTC.
        handler.formatter.converter = time.gmtime
        if not any(isinstance(existing, RedactingFilter) for existing in handler.filters):
            handler.addFilter(RedactingFilter())
