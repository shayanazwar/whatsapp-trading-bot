import pytest
from app.config import get_settings


def test_live_execution_flags_cannot_enable(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="research/paper-only"):
            get_settings()
    finally:
        monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
        get_settings.cache_clear()
