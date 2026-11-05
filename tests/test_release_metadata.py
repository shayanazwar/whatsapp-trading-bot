from pathlib import Path
import tomllib

from app.commands import HELP_TEXT
from app.version import APP_VERSION
from app.config import load_settings


def test_release_versions_are_synchronized():
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    assert pyproject["project"]["version"] == APP_VERSION == "0.8.6"
    assert env_example.startswith("# v0.8.6 S1-RFPC research build.")


def test_help_uses_the_configured_notional_limit():
    assert "gross-notional ceiling (25× by default)" in HELP_TEXT
    assert "3× wallet gross-notional ceiling" not in HELP_TEXT


def test_research_features_default_on_but_live_execution_cannot_be_unlocked(monkeypatch):
    monkeypatch.setenv("MEXC_UNIVERSE_SCAN_ENABLED", "true")
    monkeypatch.setenv("SIGNAL_PUSH_ENABLED", "true")
    monkeypatch.setenv("PAPER_TRADING_ENABLED", "true")
    monkeypatch.setenv("DATA_ALIGNMENT_PROBE_APPROVED", "false")
    monkeypatch.delenv("AUTO_TRADING_ENABLED", raising=False)
    monkeypatch.setenv("LIVE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("LIVE_EXECUTION_BUILD_RELEASED", "true")

    settings = load_settings()

    assert settings.mexc_universe_scan_enabled is True
    assert settings.signal_push_enabled is True
    assert settings.paper_trading_enabled is True
    assert settings.data_alignment_probe_approved is False
    assert settings.auto_trading_enabled is False
    assert settings.live_execution_enabled is False
    assert settings.live_execution_build_released is False
