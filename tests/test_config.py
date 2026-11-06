from __future__ import annotations

from dataclasses import replace

import pytest


def test_project_scope_rejects_other_symbols(settings):
    with pytest.raises(ValueError, match="BTC_USDT"):
        replace(settings, mexc_symbol="ETH_USDT").validate()


def test_project_scope_rejects_other_timeframes(settings):
    with pytest.raises(ValueError, match="Min1"):
        replace(settings, mexc_interval="Min5").validate()


def test_margin_budget_cannot_exceed_half_usdt(settings):
    with pytest.raises(ValueError, match="MAX_MARGIN_USDT"):
        replace(settings, max_margin_usdt=0.51).validate()


def test_invalid_sender_id_rejected(settings):
    with pytest.raises(ValueError, match="digits only"):
        replace(settings, authorized_wa_ids=("+123",)).validate()
