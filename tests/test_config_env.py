from rslr.config import _env_first, _env_list


def test_env_first_prefers_primary_over_alias(monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "primary")
    monkeypatch.setenv("META_VERIFY_TOKEN", "alias")
    assert _env_first("WHATSAPP_VERIFY_TOKEN", "META_VERIFY_TOKEN") == "primary"


def test_env_first_falls_back_to_alias(monkeypatch):
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN", raising=False)
    monkeypatch.setenv("META_VERIFY_TOKEN", "alias")
    assert _env_first("WHATSAPP_VERIFY_TOKEN", "META_VERIFY_TOKEN") == "alias"


def test_env_first_returns_default_when_all_missing(monkeypatch):
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN", raising=False)
    monkeypatch.delenv("META_VERIFY_TOKEN", raising=False)
    assert _env_first("WHATSAPP_VERIFY_TOKEN", "META_VERIFY_TOKEN") == ""


def test_env_list_parses_alias_senders(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)
    monkeypatch.setenv("WHATSAPP_ALLOWED_SENDERS", "923001234567, 923007654321")
    assert _env_list("WHATSAPP_ALLOWED_NUMBERS", "WHATSAPP_ALLOWED_SENDERS") == (
        "923001234567",
        "923007654321",
    )


def test_env_list_empty_when_all_missing(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)
    monkeypatch.delenv("WHATSAPP_ALLOWED_SENDERS", raising=False)
    assert _env_list("WHATSAPP_ALLOWED_NUMBERS", "WHATSAPP_ALLOWED_SENDERS") == ()
