from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from ..charts import ChartRenderer

LOGGER = logging.getLogger(__name__)
_ALLOWED_TIMEFRAMES = ("1D", "12H", "4H", "1H")
_ALLOWED_VERDICTS = {"SUPPORT", "VETO", "UNCERTAIN"}
_ALLOWED_ALIGNMENT = {"ALIGNED", "MIXED", "CONFLICTING", "UNCLEAR"}
_MAX_INLINE_IMAGE_BYTES = 12 * 1024 * 1024
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["SUPPORT", "VETO", "UNCERTAIN"]},
        "confidence": {"type": "INTEGER"},
        "timeframe_alignment": {
            "type": "STRING",
            "enum": ["ALIGNED", "MIXED", "CONFLICTING", "UNCLEAR"],
        },
        "reason_codes": {"type": "ARRAY", "items": {"type": "STRING"}},
        "summary": {"type": "STRING"},
    },
    "required": ["verdict", "confidence", "timeframe_alignment", "reason_codes", "summary"],
    "propertyOrdering": ["verdict", "confidence", "timeframe_alignment", "reason_codes", "summary"],
}

# These are context for a review only. Python remains authoritative for execution levels.
_ANALYSIS_FIELDS = (
    "symbol", "setup", "score", "entry", "stop_loss", "tp", "rr", "rr_gross",
    "regime_1d", "daily_structure_1d", "bias_12h", "trend_4h", "structure_label",
    "structure_12h", "structure_4h", "protected_structure_4h", "impulse_structure",
    "value_location", "liquidity_reclaim", "target_path_reason", "target_path_clear",
    "target_path_structural", "target_path_obstacles", "target_levels", "atr", "atr_1h",
    "atr_4h", "atr_percentile", "rsi_1h_entry", "relative_volume", "volume_quality",
    "close_location", "trigger_close_location", "confirmation_families_passed",
    "confirmation_families_available", "direction_ok", "structure_ok", "setup_ok",
    "confirmation_ok", "volatility_ok", "confirmation_family_diversity_ok", "location_ok",
    "risk_ok", "btc_filter_ok", "btc_filter_reason", "stop_source", "stop_loss_mode",
    "setup_state", "setup_timeframe", "primary_entry_timeframe", "mexc_spread_pct",
    "mexc_funding_rate", "futures_ok", "futures_execution_ok",
)


class GeminiChartReviewer:
    """Optional Gemini chart reviewer; never calculates or modifies trade levels.

    Modes:
      - shadow: record the model review but do not alter V11's decision.
      - gate: approve only an explicit, valid SUPPORT response; all other states veto.
    """

    def __init__(
        self,
        settings: Any,
        *,
        renderer: Any | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.api_key = str(getattr(settings, "gemini_api_key", "") or "").strip()
        model = str(getattr(settings, "gemini_model", "gemini-3.8-flash") or "gemini-3.8-flash").strip()
        self.model = model if _MODEL_RE.fullmatch(model) else "gemini-3.8-flash"
        requested_mode = str(getattr(settings, "gemini_review_mode", "shadow") or "shadow").strip().lower()
        self.mode = requested_mode if requested_mode in {"shadow", "gate"} else "gate"
        if requested_mode not in {"shadow", "gate"}:
            LOGGER.error("Invalid GEMINI_REVIEW_MODE; using fail-closed gate behavior")
        self.enabled = bool(getattr(settings, "gemini_review_enabled", False))
        self.timeout_seconds = min(90.0, max(5.0, float(getattr(settings, "gemini_timeout_seconds", 45.0) or 45.0)))
        chart_bars = int(getattr(settings, "gemini_chart_bars", 100) or 100)
        self.renderer = renderer or ChartRenderer(default_bars=max(50, min(160, chart_bars)))
        concurrency = int(getattr(settings, "gemini_max_concurrency", 2) or 2)
        self._semaphore = asyncio.Semaphore(max(1, min(2, concurrency)))
        self._render_lock = asyncio.Lock()  # matplotlib pyplot is not thread-safe.
        self._cache_ttl_seconds = max(0, min(3600, int(getattr(settings, "gemini_cache_ttl_seconds", 900) or 0)))
        self._review_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_limit = 512
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds)),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def review(
        self,
        *,
        symbol: str,
        side: str,
        analysis: dict[str, Any],
        candles: dict[str, list[Any]],
    ) -> dict[str, Any]:
        """Review a V11 technical candidate; downstream Python execution/risk checks remain authoritative."""
        if not self.enabled:
            return self._result(status="DISABLED", verdict="UNCERTAIN", summary="Gemini review is disabled")
        if not self.api_key:
            return self._result(
                status="ERROR", verdict="UNCERTAIN", summary="GEMINI_API_KEY is missing", error="missing_api_key"
            )
        if str(side or "").upper() not in {"LONG", "SHORT"}:
            return self._result(status="ERROR", verdict="UNCERTAIN", summary="Invalid candidate side", error="invalid_side")
        missing = [tf for tf in _ALLOWED_TIMEFRAMES if not isinstance(candles.get(tf), list) or len(candles[tf]) < 20]
        if missing:
            return self._result(
                status="ERROR", verdict="UNCERTAIN", summary="Not enough candles for all review charts",
                error="insufficient_candles:" + ",".join(missing),
            )

        cache_key = self._cache_key(symbol, side, analysis, candles)
        cached = self._get_cached_review(cache_key) if cache_key else None
        if cached is not None:
            return cached

        paths: list[Path] = []
        try:
            async with self._semaphore:
                image_data: list[tuple[str, bytes]] = []
                ref = SimpleNamespace(exchange="MEXC", symbol=str(symbol or "UNKNOWN").upper())
                # Render serially: matplotlib's stateful pyplot API is not thread-safe.
                async with self._render_lock:
                    for timeframe in _ALLOWED_TIMEFRAMES:
                        path = await self.renderer.render(
                            ref,
                            timeframe,
                            candles[timeframe][-max(50, min(160, int(getattr(self.settings, "gemini_chart_bars", 100) or 100))):],
                            title=f"MEXC FUTURES {str(symbol).upper()} {timeframe}",
                        )
                        paths.append(Path(path))
                        image_data.append((timeframe, await asyncio.to_thread(Path(path).read_bytes)))

                total_bytes = sum(len(data) for _, data in image_data)
                if total_bytes > _MAX_INLINE_IMAGE_BYTES:
                    return self._result(
                        status="ERROR", verdict="UNCERTAIN", summary="Chart payload exceeds safe inline limit",
                        error="chart_payload_too_large",
                    )

                prompt = self._build_prompt(symbol, side, analysis, candles)
                parts: list[dict[str, Any]] = [{"text": prompt}]
                for timeframe, raw in image_data:
                    parts.append({"text": f"Candlestick chart {timeframe}; this is a completed-candle chart."})
                    parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(raw).decode("ascii")}})

                url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
                body = {
                    "systemInstruction": {
                        "parts": [{
                            "text": (
                                "Act as a cautious independent MEXC Futures chart reviewer. Use only completed "
                                "1D, 12H, 4H, and 1H data. Never invent or modify entry, SL, TP, position size, "
                                "or risk. Do not infer unseen market data. Return only the required JSON schema; "
                                "use UNCERTAIN when evidence is ambiguous. A confidence score is not a win probability."
                            )
                        }]
                    },
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {
                        # Gemini 3 models count thinking tokens against maxOutputTokens; leave enough headroom
                        # for chart reasoning and the structured response. Avoid temperature overrides.
                        "thinkingConfig": {"thinkingLevel": "high"},
                        "maxOutputTokens": 4096,
                        "responseMimeType": "application/json",
                        "responseSchema": _RESPONSE_SCHEMA,
                    },
                }
                try:
                    response = await asyncio.wait_for(
                        self._client.post(
                            url,
                            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                            json=body,
                        ),
                        timeout=self.timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    return self._result(
                        status="ERROR", verdict="UNCERTAIN", summary="Gemini request timed out", error="timeout"
                    )
                except Exception as exc:
                    LOGGER.warning("Gemini request failed for %s: %s", symbol, type(exc).__name__)
                    return self._result(
                        status="ERROR", verdict="UNCERTAIN", summary="Gemini request failed",
                        error=f"request_error:{type(exc).__name__}",
                    )

                status_code = int(getattr(response, "status_code", 0) or 0)
                if status_code < 200 or status_code >= 300:
                    LOGGER.warning("Gemini API returned HTTP %s for %s", status_code, symbol)
                    return self._result(
                        status="ERROR", verdict="UNCERTAIN", summary=f"Gemini API HTTP {status_code}",
                        error=f"http_{status_code}",
                    )
                try:
                    payload = response.json()
                except Exception:
                    return self._result(
                        status="ERROR", verdict="UNCERTAIN", summary="Gemini returned invalid JSON envelope",
                        error="invalid_response_json",
                    )
                parsed = self._extract_response(payload)
                if parsed is None:
                    return self._result(
                        status="ERROR", verdict="UNCERTAIN", summary="Gemini response did not match required schema",
                        error="invalid_model_output",
                    )
                result = self._result(status="OK", **parsed)
                if cache_key and self._cache_ttl_seconds > 0:
                    self._store_cached_review(cache_key, result)
                return result
        except Exception as exc:
            LOGGER.exception("Gemini chart review failed for %s", symbol)
            return self._result(
                status="ERROR", verdict="UNCERTAIN", summary="Chart review failed", error=f"review_error:{type(exc).__name__}"
            )
        finally:
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("Could not remove temporary Gemini chart: %s", path.name)

    def _cache_key(
        self, symbol: str, side: str, analysis: dict[str, Any], candles: dict[str, list[Any]]
    ) -> str | None:
        chart_bars = max(50, min(160, int(getattr(self.settings, "gemini_chart_bars", 100) or 100)))
        candle_snapshot: dict[str, Any] = {}
        for timeframe in _ALLOWED_TIMEFRAMES:
            rows = [self._normalize_candle(row) for row in candles.get(timeframe, [])[-chart_bars:]]
            rows = [row for row in rows if row is not None]
            if len(rows) < 20:
                return None
            candle_snapshot[timeframe] = rows
        candidate = {key: self._json_safe(analysis[key]) for key in _ANALYSIS_FIELDS if key in analysis}
        # Key by all model-visible facts, candle history, and model name so a changed
        # context cannot accidentally reuse a stale review.
        snapshot = {
            "model": self.model, "symbol": str(symbol).upper(), "side": str(side).upper(),
            "candidate": candidate, "candles": candle_snapshot,
        }
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _get_cached_review(self, key: str) -> dict[str, Any] | None:
        if self._cache_ttl_seconds <= 0:
            return None
        now = time.monotonic()
        item = self._review_cache.get(key)
        if item is None:
            return None
        created_at, result = item
        if now - created_at >= self._cache_ttl_seconds:
            self._review_cache.pop(key, None)
            return None
        return {**result, "cached": True}

    def _store_cached_review(self, key: str, result: dict[str, Any]) -> None:
        now = time.monotonic()
        # Prune expired entries first, then evict the oldest if the cap is reached.
        for old_key, (created_at, _) in list(self._review_cache.items()):
            if now - created_at >= self._cache_ttl_seconds:
                self._review_cache.pop(old_key, None)
        while len(self._review_cache) >= self._cache_limit:
            oldest_key = min(self._review_cache, key=lambda cache_key: self._review_cache[cache_key][0])
            self._review_cache.pop(oldest_key, None)
        self._review_cache[key] = (now, {**result, "cached": False})

    @classmethod
    def _build_prompt(
        cls,
        symbol: str,
        side: str,
        analysis: dict[str, Any],
        candles: dict[str, list[Any]],
    ) -> str:
        context: dict[str, Any] = {}
        for key in _ANALYSIS_FIELDS:
            if key in analysis:
                context[key] = cls._json_safe(analysis[key])
        recent_candles: dict[str, Any] = {}
        candle_count = {"1D": 18, "12H": 24, "4H": 30, "1H": 30}
        for timeframe in _ALLOWED_TIMEFRAMES:
            normalized = [cls._normalize_candle(row) for row in candles[timeframe][-candle_count[timeframe]:]]
            recent_candles[timeframe] = [row for row in normalized if row is not None]

        input_json = json.dumps(
            {
                "symbol": str(symbol).upper(),
                "candidate_side": str(side).upper(),
                "python_v11_candidate_metrics": context,
                "recent_completed_candles": {
                    "columns": ["timestamp_ms", "open", "high", "low", "close", "volume"],
                    "timeframes": recent_candles,
                },
            },
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return (
            "You are an independent chart reviewer for a rule-based MEXC Futures swing strategy. "
            "Review ONLY the supplied MEXC charts and numerical completed-candle data for 1D, 12H, 4H, and 1H. "
            "Do not use or request any other timeframe. The charts are ordered 1D, 12H, 4H, 1H.\n\n"
            "Your task is to challenge, not invent, the candidate generated by Python V11. Check: "
            "(1) whether higher-timeframe regime/bias conflicts with the candidate; "
            "(2) whether 4H structure and value location support the direction; "
            "(3) whether 1H structure shows a credible trigger rather than an extended/late entry; "
            "(4) obvious nearby opposing levels/target-path obstacles; "
            "(5) candle/chart and numerical-data consistency.\n\n"
            "Never create or alter entry, stop loss, take profit, leverage, position size, or risk. "
            "Do not infer unseen order-book, funding, news, or future price data. Do not claim certainty. "
            "Use VETO only for a material visible conflict, broken structure, clearly obstructed target path, "
            "or evidence that strongly contradicts the candidate. Use SUPPORT when no material conflict is visible. "
            "Use UNCERTAIN when the charts/data are ambiguous, insufficient, or inconsistent. "
            "Confidence is review confidence, not probability of profit. Keep summary concise and reason_codes factual. "
            "Return only the required JSON schema.\n\nCANDIDATE DATA:\n" + input_json
        )

    @staticmethod
    def _normalize_candle(row: Any) -> list[int | float] | None:
        try:
            if isinstance(row, dict):
                timestamp = next(
                    (row.get(key) for key in ("time", "timestamp", "openTime", "ts") if row.get(key) is not None),
                    None,
                )
                volume_value = row.get("volume") if row.get("volume") is not None else row.get("vol", 0)
                values = [timestamp, row.get("open"), row.get("high"), row.get("low"), row.get("close"), volume_value]
            else:
                if not isinstance(row, (list, tuple)) or len(row) < 6:
                    return None
                values = list(row[:6])
            ts = int(float(values[0]))
            if ts < 10**12:
                ts *= 1000
            o, h, low, close = (float(v) for v in values[1:5])
            volume = float(values[5] or 0.0)
            if not all(math.isfinite(v) for v in (o, h, low, close, volume)):
                return None
            if ts <= 0 or min(o, h, low, close) <= 0 or volume < 0 or low > h:
                return None
            if low > min(o, close) or h < max(o, close):
                return None
            return [ts, o, h, low, close, volume]
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    @classmethod
    def _json_safe(cls, value: Any, depth: int = 0) -> Any:
        if depth > 4:
            return str(value)[:160]
        if value is None or isinstance(value, (str, bool, int)):
            return value if not isinstance(value, str) else value[:500]
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            return {str(k)[:80]: cls._json_safe(v, depth + 1) for k, v in list(value.items())[:20]}
        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe(v, depth + 1) for v in list(value)[:12]]
        return str(value)[:160]

    @staticmethod
    def _extract_response(payload: Any) -> dict[str, Any] | None:
        try:
            candidates = payload.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                return None
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            raw_text = "".join(
                str(part.get("text") or "")
                for part in parts
                if isinstance(part, dict) and not bool(part.get("thought", False))
            ).strip()
            if not raw_text:
                return None
            parsed = json.loads(raw_text)
            verdict = str(parsed.get("verdict") or "").strip().upper()
            alignment = str(parsed.get("timeframe_alignment") or "").strip().upper()
            if verdict not in _ALLOWED_VERDICTS or alignment not in _ALLOWED_ALIGNMENT:
                return None
            if verdict == "SUPPORT" and alignment == "CONFLICTING":
                return None
            raw_confidence = parsed.get("confidence")
            if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, int):
                return None
            confidence = raw_confidence
            if not 0 <= confidence <= 100:
                return None
            raw_reasons = parsed.get("reason_codes")
            if not isinstance(raw_reasons, list) or not all(isinstance(item, str) for item in raw_reasons):
                return None
            reasons = [item.strip()[:100] for item in raw_reasons if item.strip()][:8]
            summary = parsed.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                return None
            return {
                "verdict": verdict,
                "confidence": confidence,
                "timeframe_alignment": alignment,
                "reason_codes": reasons,
                "summary": summary.strip()[:500],
            }
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _result(
        self,
        *,
        status: str,
        verdict: str,
        summary: str,
        error: str = "",
        confidence: int = 0,
        timeframe_alignment: str = "UNCLEAR",
        reason_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": str(status),
            "verdict": str(verdict),
            "confidence": max(0, min(100, int(confidence))),
            "timeframe_alignment": str(timeframe_alignment),
            "reason_codes": list(reason_codes or [])[:8],
            "summary": str(summary)[:500],
            "error": str(error)[:160],
            "model": self.model,
            "cached": False,
        }
