from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.automation.scanner as scanner_module
from app.automation.gemini_reviewer import GeminiChartReviewer
from app.automation.scanner import MexcScanner
from app.config import Settings


TIMEFRAMES = ("1D", "12H", "4H", "1H")


def _candles(count: int = 220) -> list[dict]:
    base = 1_700_000_000_000
    return [
        {
            "time": base + index * 3_600_000,
            "open": 100.0 + index * 0.01,
            "high": 101.0 + index * 0.01,
            "low": 99.0 + index * 0.01,
            "close": 100.5 + index * 0.01,
            "volume": 1000.0 + index,
        }
        for index in range(count)
    ]


def _gemini_output(verdict: str = "SUPPORT") -> dict:
    return {
        "verdict": verdict,
        "confidence": 73,
        "timeframe_alignment": "ALIGNED",
        "reason_codes": ["HTF_ALIGNMENT", "NO_CLEAR_OBSTACLE"],
        "summary": "No material conflict is visible in the supplied charts.",
    }


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class _FakeHTTPClient:
    def __init__(self, response=None, error=None):
        self.response = response or _FakeResponse({"candidates": [{"content": {"parts": [{"text": json.dumps(_gemini_output())}]}}]})
        self.error = error
        self.calls = []

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.error:
            raise self.error
        return self.response


class _FakeRenderer:
    def __init__(self, root: Path):
        self.root = root
        self.calls: list[str] = []

    async def render(self, ref, timeframe, rows, title=None):
        self.calls.append(timeframe)
        path = self.root / f"{timeframe}.png"
        path.write_bytes(f"fake-image-{timeframe}".encode())
        return path


def test_candle_normalizer_rejects_invalid_ohlc_and_accepts_second_timestamp():
    valid = GeminiChartReviewer._normalize_candle([1_700_000_000, 10, 12, 9, 11, 50])
    assert valid == [1_700_000_000_000, 10.0, 12.0, 9.0, 11.0, 50.0]
    assert GeminiChartReviewer._normalize_candle([1_700_000_000, 10, 9, 8, 11, 50]) is None
    assert GeminiChartReviewer._normalize_candle({"time": 1, "open": float("nan"), "high": 3, "low": 1, "close": 2, "volume": 1}) is None


def test_prompt_uses_only_approved_timeframes_and_never_asks_ai_to_set_levels():
    candles = {tf: _candles(40) for tf in TIMEFRAMES}
    prompt = GeminiChartReviewer._build_prompt(
        "BTC_USDT", "LONG", {"entry": 100, "stop_loss": 95, "tp": 110, "rr": 2}, candles
    )
    assert all(tf in prompt for tf in TIMEFRAMES)
    assert "Never create or alter entry, stop loss, take profit" in prompt
    assert "recent_completed_candles" in prompt
    assert '"candidate_side":"LONG"' in prompt


def test_review_sends_four_chart_images_and_validates_structured_response(tmp_path):
    http = _FakeHTTPClient()
    renderer = _FakeRenderer(tmp_path)
    settings = Settings(gemini_api_key="test-key", gemini_review_enabled=True, gemini_model="gemini-3.8-flash")
    reviewer = GeminiChartReviewer(settings, renderer=renderer, http_client=http)
    candles = {tf: _candles(40) for tf in TIMEFRAMES}

    async def review_twice():
        first = await reviewer.review(symbol="BTC_USDT", side="LONG", analysis={"entry": 100, "stop_loss": 95, "tp": 110, "rr": 2}, candles=candles)
        second = await reviewer.review(symbol="BTC_USDT", side="LONG", analysis={"entry": 100, "stop_loss": 95, "tp": 110, "rr": 2}, candles=candles)
        return first, second

    result, cached_result = asyncio.run(review_twice())

    assert result["status"] == "OK"
    assert cached_result["status"] == "OK" and cached_result["cached"] is True
    assert result["verdict"] == "SUPPORT"
    assert result["model"] == "gemini-3.8-flash"
    assert renderer.calls == list(TIMEFRAMES)
    assert len(http.calls) == 1
    assert renderer.calls == list(TIMEFRAMES)  # cache hit avoids re-rendering
    call = http.calls[0]
    assert call["headers"]["x-goog-api-key"] == "test-key"
    assert call["url"].endswith("/models/gemini-3.8-flash:generateContent")
    parts = call["json"]["contents"][0]["parts"]
    assert sum(1 for part in parts if "inline_data" in part) == 4
    assert call["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert call["json"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high"
    assert call["json"]["generationConfig"]["maxOutputTokens"] == 4096
    assert "temperature" not in call["json"]["generationConfig"]
    assert "Never invent or modify entry" in call["json"]["systemInstruction"]["parts"][0]["text"]
    assert not any((tmp_path / f"{tf}.png").exists() for tf in TIMEFRAMES)


def test_conflicting_support_response_is_rejected_as_invalid_output():
    data = _gemini_output("SUPPORT")
    data["timeframe_alignment"] = "CONFLICTING"
    payload = {"candidates": [{"content": {"parts": [{"text": json.dumps(data)}]}}]}
    assert GeminiChartReviewer._extract_response(payload) is None


def test_invalid_model_output_is_uncertain_error(tmp_path):
    http = _FakeHTTPClient(response=_FakeResponse({"candidates": [{"content": {"parts": [{"text": "not-json"}]}}]}))
    reviewer = GeminiChartReviewer(
        Settings(gemini_api_key="test-key", gemini_review_enabled=True),
        renderer=_FakeRenderer(tmp_path), http_client=http,
    )
    result = asyncio.run(reviewer.review(
        symbol="BTC_USDT", side="LONG", analysis={}, candles={tf: _candles(40) for tf in TIMEFRAMES}
    ))
    assert result["status"] == "ERROR"
    assert result["verdict"] == "UNCERTAIN"
    assert result["error"] == "invalid_model_output"


def test_http_error_returns_uncertain_and_cleans_temporary_charts(tmp_path):
    http = _FakeHTTPClient(response=_FakeResponse({"error": {"message": "rate limited"}}, status_code=429))
    reviewer = GeminiChartReviewer(
        Settings(gemini_api_key="test-key", gemini_review_enabled=True),
        renderer=_FakeRenderer(tmp_path), http_client=http,
    )
    result = asyncio.run(reviewer.review(
        symbol="BTC_USDT", side="LONG", analysis={}, candles={tf: _candles(40) for tf in TIMEFRAMES}
    ))
    assert result["status"] == "ERROR" and result["verdict"] == "UNCERTAIN"
    assert result["error"] == "http_429"
    assert not any((tmp_path / f"{tf}.png").exists() for tf in TIMEFRAMES)


def test_timeout_returns_uncertain_without_raising(tmp_path):
    http = _FakeHTTPClient(error=asyncio.TimeoutError())
    reviewer = GeminiChartReviewer(
        Settings(gemini_api_key="test-key", gemini_review_enabled=True, gemini_timeout_seconds=5),
        renderer=_FakeRenderer(tmp_path), http_client=http,
    )
    result = asyncio.run(reviewer.review(
        symbol="BTC_USDT", side="LONG", analysis={}, candles={tf: _candles(40) for tf in TIMEFRAMES}
    ))
    assert result["status"] == "ERROR" and result["verdict"] == "UNCERTAIN"
    assert result["error"] == "timeout"
    assert not any((tmp_path / f"{tf}.png").exists() for tf in TIMEFRAMES)


def test_missing_api_key_fails_safely_without_rendering(tmp_path):
    renderer = _FakeRenderer(tmp_path)
    reviewer = GeminiChartReviewer(Settings(gemini_review_enabled=True), renderer=renderer, http_client=_FakeHTTPClient())
    result = asyncio.run(reviewer.review(
        symbol="BTC_USDT", side="LONG", analysis={}, candles={tf: _candles(40) for tf in TIMEFRAMES}
    ))
    assert result["status"] == "ERROR"
    assert result["error"] == "missing_api_key"
    assert renderer.calls == []


class _FakeReviewer:
    def __init__(self, result: dict):
        self.result = result
        self.calls = []

    async def review(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


class _FakeSignalManager:
    async def publish(self, signal):
        return False


class _FakeClient:
    def __init__(self):
        self.ticker_calls = 0

    async def get_ticker(self, symbol):
        self.ticker_calls += 1
        return {"lastPrice": 100.0, "bid1": 100.0, "ask1": 100.0}


class _FakeUniverse:
    async def refresh(self):
        return ["TEST_USDT"]


def _candidate_analysis() -> dict:
    return {
        "symbol": "TEST_USDT", "setup": "LONG", "score": 80, "entry": 100.0,
        "stop_loss": 99.0, "tp": 104.0, "rr": 3.0, "direction_ok": True,
        "structure_ok": True, "setup_ok": True, "confirmation_ok": True,
        "volatility_ok": True, "confirmation_family_diversity_ok": True,
        "location_ok": True, "target_path_structural": True, "target_path_clear": True,
        "risk_ok": True, "technical_candidate": True, "btc_filter_ok": True,
        "btc_filter_reason": "OK", "stage_status": {"1D_REGIME": True, "12H_BIAS": True,
            "1H_TRIGGER": True, "RISK": True, "RR": True, "QUALITY": True},
        "stage_failures": {}, "diagnostic_failures": [], "primary_entry_timeframe": "1H",
        "signal_candle_timeframe": "1H", "candle_close_time": int(time.time() * 1000),
    }


def _scanner_for_mode(monkeypatch, mode: str, verdict: str, confidence: int = 80, alignment: str = "ALIGNED"):
    rows_1d = _candles(220)
    rows_12h = _candles(80)
    rows_4h = _candles(650)
    rows_1h = _candles(250)
    monkeypatch.setattr(scanner_module, "analyze_candles", lambda *args, **kwargs: _candidate_analysis())
    monkeypatch.setattr(scanner_module, "synthesize_12h_from_4h", lambda rows, **kwargs: rows_12h)
    monkeypatch.setattr(scanner_module, "btc_filter_ok", lambda *args, **kwargs: (True, "OK"))
    monkeypatch.setattr(
        MexcScanner, "_ticker_quality",
        staticmethod(lambda ticker, age, *, max_spread_pct: (True, "OK", 0.0)),
    )
    fake_client = _FakeClient()
    reviewer_result = {
        "status": "OK", "verdict": verdict, "confidence": confidence,
        "timeframe_alignment": alignment, "reason_codes": ["TEST"],
        "summary": "test result", "error": "", "model": "test-model",
    }
    reviewer = _FakeReviewer(reviewer_result)
    settings = Settings(gemini_review_enabled=True, gemini_review_mode=mode, auto_signal_enabled=False)
    scanner = MexcScanner(
        settings=settings, client=fake_client, universe=_FakeUniverse(),
        signal_manager=_FakeSignalManager(), gemini_reviewer=reviewer,
    )

    async def fake_get_closed(symbol, timeframe, limit):
        return {"1D": rows_1d, "12H": rows_12h, "4H": rows_4h, "1H": rows_1h}[timeframe], False

    async def fake_futures_context(symbol, ticker, *, spread_pct):
        return {"execution_ok": True, "best_ask": 100.0, "best_bid": 100.0, "last_price": 100.0, "funding_rate": 0.0}

    monkeypatch.setattr(scanner, "_get_closed_candles", fake_get_closed)
    monkeypatch.setattr(scanner, "_get_futures_execution_context", fake_futures_context)
    monkeypatch.setattr(scanner, "_reprice_for_next_1h_open", lambda *args, **kwargs: (True, "OK"))
    monkeypatch.setattr(scanner_module, "validate_signal", lambda data, **kwargs: (SimpleNamespace(symbol="TEST_USDT", analysis=data), []))
    return scanner, fake_client, reviewer


@pytest.mark.parametrize(
    ("mode", "verdict", "confidence", "alignment", "should_reject"),
    [
        ("gate", "VETO", 80, "ALIGNED", True),
        ("shadow", "VETO", 80, "ALIGNED", False),
        ("gate", "SUPPORT", 50, "ALIGNED", True),
        ("gate", "SUPPORT", 80, "CONFLICTING", True),
        ("gate", "SUPPORT", 80.9, "ALIGNED", True),
        ("gate", "SUPPORT", 80, "ALIGNED", False),
    ],
)
def test_scanner_gemini_gate_requires_valid_confident_nonconflicting_support(
    monkeypatch, mode, verdict, confidence, alignment, should_reject
):
    scanner, client, reviewer = _scanner_for_mode(monkeypatch, mode, verdict, confidence, alignment)

    async def scan_and_drain():
        result = await scanner._scan_one("TEST_USDT")
        # Fake reviews complete quickly; drain so assertions don't depend on event-loop teardown.
        await scanner.close_gemini_shadow_reviews(timeout_seconds=1.0)
        return result

    result = asyncio.run(scan_and_drain())
    assert len(reviewer.calls) == 1
    assert set(reviewer.calls[0]["candles"]) == set(TIMEFRAMES)
    if should_reject:
        assert result["rejection_stage"] == "GEMINI_REVIEW"
        assert result["rejected_gemini"] == 1
        assert client.ticker_calls == 0  # A veto stops before execution requests.
    else:
        assert result["valid"] is True
        assert result["final_pass"] == 1
        assert client.ticker_calls == 1
        if mode == "shadow":
            assert result["gemini_queued"] == 1
            assert "gemini_review" not in result["analysis"]
            assert result["gemini_veto_suggestions"] == 0
        else:
            assert result["analysis"]["gemini_review"]["verdict"] == verdict
            assert result["gemini_veto_suggestions"] == int(verdict == "VETO")


def test_shadow_mode_continues_if_reviewer_raises(monkeypatch):
    scanner, client, _ = _scanner_for_mode(monkeypatch, "shadow", "SUPPORT")

    class BrokenReviewer:
        async def review(self, **kwargs):
            raise RuntimeError("mock reviewer failure")

    scanner.gemini_reviewer = BrokenReviewer()

    async def scan_and_drain():
        result = await scanner._scan_one("TEST_USDT")
        await scanner.close_gemini_shadow_reviews(timeout_seconds=1.0)
        return result

    result = asyncio.run(scan_and_drain())
    assert result["valid"] is True
    assert "gemini_review" not in result["analysis"]
    assert result["gemini_queued"] == 1
    assert client.ticker_calls == 1


def test_shadow_review_does_not_wait_for_a_slow_model(monkeypatch):
    scanner, client, _ = _scanner_for_mode(monkeypatch, "shadow", "SUPPORT")

    class SlowReviewer:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def review(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return {
                "status": "OK", "verdict": "VETO", "confidence": 90,
                "timeframe_alignment": "ALIGNED", "reason_codes": ["TEST"],
                "summary": "Delayed review result",
            }

    slow = SlowReviewer()
    scanner.gemini_reviewer = slow

    async def run():
        result = await asyncio.wait_for(scanner._scan_one("TEST_USDT"), timeout=1.0)
        # Let the background task start, but leave it blocked to prove scan completion is independent.
        await asyncio.sleep(0)
        assert slow.started.is_set()
        assert not slow.release.is_set()
        assert result["valid"] is True
        assert result["gemini_queued"] == 1
        assert "gemini_review" not in result["analysis"]
        assert client.ticker_calls == 1
        slow.release.set()
        await scanner.close_gemini_shadow_reviews(timeout_seconds=1.0)
        return result

    result = asyncio.run(run())
    assert result["valid"] is True


def test_gate_mode_without_gemini_enabled_fails_closed(monkeypatch):
    analysis = _candidate_analysis()
    rows_1d, rows_12h, rows_4h, rows_1h = _candles(220), _candles(80), _candles(650), _candles(250)
    monkeypatch.setattr(scanner_module, "analyze_candles", lambda *args, **kwargs: analysis)
    monkeypatch.setattr(scanner_module, "synthesize_12h_from_4h", lambda rows, **kwargs: rows_12h)
    monkeypatch.setattr(scanner_module, "btc_filter_ok", lambda *args, **kwargs: (True, "OK"))
    settings = Settings(gemini_review_enabled=False, gemini_review_mode="gate")
    scanner = MexcScanner(settings=settings, client=_FakeClient(), universe=_FakeUniverse(), signal_manager=_FakeSignalManager())

    async def fake_get_closed(symbol, timeframe, limit):
        return {"1D": rows_1d, "12H": rows_12h, "4H": rows_4h, "1H": rows_1h}[timeframe], False

    monkeypatch.setattr(scanner, "_get_closed_candles", fake_get_closed)
    result = asyncio.run(scanner._scan_one("TEST_USDT"))
    assert result["rejection_stage"] == "GEMINI_REVIEW"
    assert result["rejected_gemini"] == 1


def test_whatsapp_signal_discloses_shadow_veto_without_changing_signal_levels():
    from app.automation.signal_manager import format_signal

    signal = SimpleNamespace(
        symbol="TEST_USDT",
        side="LONG",
        plan=SimpleNamespace(entry=100.0, stop_loss=99.0, tp=104.0, rr=3.0),
        analysis={
            "score": 80,
            "regime_1d": "BULLISH",
            "bias_12h": "BULLISH",
            "structure_4h": "BULLISH",
            "trigger_type": "SWEEP_RECLAIM",
            "gemini_review": {
                "mode": "shadow", "status": "OK", "verdict": "VETO", "confidence": 82,
                "summary": "A visible opposing level may obstruct the target.",
            },
        },
    )
    message = format_signal(signal)
    assert "Gemini Chart Review (SHADOW): VETO · review confidence 82/100" in message
    assert "shadow mode does not block this signal" in message
    assert "Entry (NEXT 1H OPEN MARKET): $100" in message
    assert "SL: $99" in message
    assert "TP: $104" in message


def test_real_chart_renderer_supports_all_four_gemini_timeframes():
    from app.charts import ChartRenderer

    renderer = ChartRenderer(default_bars=50)
    ref = SimpleNamespace(exchange="MEXC", symbol="TEST_USDT")

    async def render_all():
        paths = []
        for timeframe in TIMEFRAMES:
            paths.append(await renderer.render(ref, timeframe, _candles(40), title=f"test {timeframe}"))
        return paths

    paths = asyncio.run(render_all())
    try:
        assert len(paths) == 4
        assert all(Path(path).exists() and Path(path).stat().st_size > 0 for path in paths)
    finally:
        for path in paths:
            Path(path).unlink(missing_ok=True)
