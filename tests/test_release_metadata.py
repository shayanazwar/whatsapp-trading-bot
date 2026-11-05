from pathlib import Path
import tomllib

from app.commands import HELP_TEXT
from app.version import APP_VERSION


def test_release_versions_are_synchronized():
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    assert pyproject["project"]["version"] == APP_VERSION == "0.8.4"
    assert env_example.startswith("# v0.8.4 S1-RFPC research build.")


def test_help_uses_the_configured_notional_limit():
    assert "gross-notional ceiling (25× by default)" in HELP_TEXT
    assert "3× wallet gross-notional ceiling" not in HELP_TEXT
