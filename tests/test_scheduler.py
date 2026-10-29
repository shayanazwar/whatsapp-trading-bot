from __future__ import annotations

import json
from dataclasses import replace

from app.config import load_settings
from app.mexc.client import MexcClient
from app.services.scheduler import ResearchScheduler
from app.storage import Store


class DummyWhatsApp:
    async def aclose(self):
        return None



def test_paper_scheduler_requires_explicit_alignment_and_price_semantics_review(tmp_path):
    base = load_settings()
    reports = tmp_path / "reports"
    reports.mkdir()
    settings = replace(base, reports_dir=reports, data_alignment_probe_approved=True,
                       paper_trading_enabled=True, default_symbols=("BTC_USDT", "ETH_USDT"))
    report = {
        "status": "COMPLETE_WITH_OBSERVATIONS",
        "symbols_requested": ["BTC_USDT", "ETH_USDT"],
        "symbols": {"BTC_USDT": {"status": "observed"}, "ETH_USDT": {"status": "observed"}},
        "alignment_gates_passed": True,
        "price_series_semantics_reviewed": False,
    }
    (reports / "alignment_probe.json").write_text(json.dumps(report), encoding="utf-8")
    store = Store(tmp_path / "scheduler.sqlite3")
    mexc = MexcClient(max_retries=0)
    scheduler = ResearchScheduler(settings, store, mexc, DummyWhatsApp())
    ok, reason = scheduler.alignment_gate_passed()
    assert ok is False
    assert "semantics" in reason

    report["price_series_semantics_reviewed"] = True
    (reports / "alignment_probe.json").write_text(json.dumps(report), encoding="utf-8")
    ok, reason = scheduler.alignment_gate_passed()
    assert ok is True
    assert "passed" in reason


def test_paper_scheduler_gate_fails_when_one_requested_symbol_failed(tmp_path):
    base = load_settings()
    reports = tmp_path / "reports"
    reports.mkdir()
    settings = replace(base, reports_dir=reports, data_alignment_probe_approved=True,
                       paper_trading_enabled=True, default_symbols=("BTC_USDT", "ETH_USDT"))
    report = {
        "status": "COMPLETE_WITH_OBSERVATIONS",
        "symbols_requested": ["BTC_USDT", "ETH_USDT"],
        "symbols": {"BTC_USDT": {"status": "observed"}, "ETH_USDT": {"status": "failed"}},
        "alignment_gates_passed": True,
        "price_series_semantics_reviewed": True,
    }
    (reports / "alignment_probe.json").write_text(json.dumps(report), encoding="utf-8")
    store = Store(tmp_path / "scheduler.sqlite3")
    mexc = MexcClient(max_retries=0)
    scheduler = ResearchScheduler(settings, store, mexc, DummyWhatsApp())
    ok, reason = scheduler.alignment_gate_passed()
    assert ok is False
    assert "every requested symbol" in reason
