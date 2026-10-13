from __future__ import annotations

import asyncio
from pathlib import Path

from app import main
from app.analysis import engine
from app.automation.gemini_reviewer import GeminiChartReviewer
from app.backtest.report import format_report, summarize
from app.backtest.reproducibility import _source_fingerprint
from app.config import Settings


def test_optional_gemini_reviewer_is_wired_to_runtime_scanner():
    assert main.mexc_scanner.gemini_reviewer is main.gemini_reviewer


def test_gemini_reviewer_respects_disabled_default():
    assert main._build_gemini_reviewer(Settings(gemini_review_enabled=False)) is None


def test_enabled_gemini_reviewer_can_be_constructed_without_network_call():
    reviewer = main._build_gemini_reviewer(
        Settings(gemini_review_enabled=True, gemini_api_key="unit-test-placeholder")
    )
    assert isinstance(reviewer, GeminiChartReviewer)
    assert reviewer.model == "gemini-3.8-flash"
    asyncio.run(reviewer.aclose())


def test_shutdown_closes_the_runtime_gemini_reviewer(monkeypatch):
    calls = []

    class FakeReviewer:
        async def aclose(self):
            calls.append("closed")

    async def noop():
        return None

    monkeypatch.setattr(main, "gemini_reviewer", FakeReviewer())
    for target, attribute in (
        (main.scanner_scheduler, "stop"),
        (main.mexc_scanner, "close_gemini_shadow_reviews"),
        (main.paper_trader, "stop"),
        (main.alert_engine, "stop"),
        (main.market, "close"),
        (main.mexc_client, "close"),
        (main.whatsapp, "close"),
    ):
        monkeypatch.setattr(target, attribute, noop)

    asyncio.run(main.shutdown_event())
    assert calls == ["closed"]


def test_backtest_source_fingerprint_includes_imported_indicators(tmp_path: Path):
    before, _ = _source_fingerprint(tmp_path)
    indicator_path = tmp_path / "app" / "analysis" / "indicators.py"
    indicator_path.parent.mkdir(parents=True)
    indicator_path.write_text("def ema(values, period): return values[-1]\n", encoding="utf-8")

    after, files = _source_fingerprint(tmp_path)
    assert "app/analysis/indicators.py" in files
    assert files["app/analysis/indicators.py"] != "MISSING"
    assert after != before


def test_report_prints_exact_utc_bounds_and_actual_elapsed_duration():
    start_ms = 1_790_697_600_000
    end_ms = start_ms + 7 * 86_400_000
    report = format_report(summarize(
        days=1,
        period_start_ms=start_ms,
        period_end_ms=end_ms,
        coins_selected=0,
        coins_tested=0,
        data_errors=0,
        execution_errors=0,
        rejected_setups=0,
        trades=[],
        diagnostics={"FIXED_BACKTEST_PERIOD_ENABLED": 1},
    ))
    assert "2026-09-29T16:00:00Z" in report
    assert "2026-10-06T16:00:00Z" in report
    assert "elapsed: 7.000000 days" in report
    assert "requested 1D" in report
