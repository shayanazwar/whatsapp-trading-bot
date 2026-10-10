from __future__ import annotations

"""Frozen-input snapshot/replay and trade-ledger support for V11 backtests.

This module is opt-in. With neither V11_BACKTEST_SNAPSHOT_DIR nor
V11_BACKTEST_REPLAY_DIR set, it does not change backtest behavior.
"""

import dataclasses
import gzip
import hashlib
import json
import logging
import os
import re
import sys
import platform
import importlib.metadata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..analysis.engine import convert_candles

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _candle_row(row: Any) -> list[Any]:
    """Return stable OHLCV ordering from the runner's normalized candle row."""
    if isinstance(row, Mapping):
        get = row.get
        ts = get("time", get("timestamp", get("openTime", get("ts"))))
        return [
            int(float(ts)),
            float(get("open", get("o"))),
            float(get("high", get("h"))),
            float(get("low", get("l"))),
            float(get("close", get("c"))),
            float(get("volume", get("vol", get("q", 0.0))) or 0.0),
        ]
    return [int(float(row[0])), *(float(x) for x in row[1:6])]


def _source_fingerprint(project_root: Path) -> tuple[str, dict[str, str]]:
    relative_paths = (
        "app/backtest/runner.py",
        "app/backtest/reproducibility.py",
        "app/backtest/simulator.py",
        "app/backtest/report.py",
        "app/analysis/engine.py",
        "app/automation/universe.py",
        "app/automation/mexc_client.py",
        "app/config.py",
    )
    file_hashes: dict[str, str] = {}
    for relative in relative_paths:
        path = project_root / relative
        if path.is_file():
            file_hashes[relative] = _sha256(path.read_bytes())
        else:
            file_hashes[relative] = "MISSING"
    aggregate = _sha256(_canonical_json_bytes(file_hashes))
    return aggregate, file_hashes


def _safe_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _safe_value(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)



def _runtime_info() -> dict[str, Any]:
    packages = {}
    for name in ("pandas", "numpy", "pydantic", "fastapi", "httpx"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": packages,
    }

def _settings_snapshot(settings: Any) -> dict[str, Any]:
    """Whitelist only research-relevant, non-secret configuration fields."""
    names = (
        "backtest_max_symbols",
        "backtest_symbol_concurrency",
        "backtest_analysis_timeout_seconds",
        "backtest_max_open_positions",
        "backtest_max_same_direction",
        "backtest_total_open_risk_r",
        "backtest_fee_rate",
        "backtest_slippage_bps",
        "estimated_round_trip_cost_pct",
        "estimated_funding_cost_pct",
        "backtest_short_shadow_enabled",
        "allow_live_execution",
    )
    return {name: _safe_value(getattr(settings, name, None)) for name in names}


class BacktestReproStore:
    """Captures normalized market inputs or loads an immutable replay fixture."""

    def __init__(
        self,
        *,
        mode: str,
        path: Path,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        self.mode = mode
        self.path = path
        self.manifest = manifest or {}
        self._files: dict[str, dict[str, Any]] = {}
        self._selected_symbols: list[str] = []

    @classmethod
    def capture(cls, parent: str, *, start_ms: int, end_ms: int) -> "BacktestReproStore":
        root = Path(parent).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = root / f"snapshot_{stamp}_{run_id}"
        (path / "histories").mkdir(parents=True, exist_ok=False)
        store = cls(mode="capture", path=path)
        store.manifest = {"period_start_ms": int(start_ms), "period_end_ms": int(end_ms)}
        LOGGER.info("BACKTEST SNAPSHOT CAPTURE START | path=%s", path)
        return store

    @classmethod
    def replay(cls, path: str, *, start_ms: int, end_ms: int) -> "BacktestReproStore":
        snapshot_path = Path(path).expanduser().resolve()
        manifest_path = snapshot_path / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"Backtest replay manifest not found: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read backtest replay manifest: {exc}") from exc
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported backtest snapshot schema version")
        if manifest.get("complete") is not True:
            raise ValueError("Backtest snapshot is incomplete; recapture a clean dataset")
        if int(manifest.get("period_start_ms", -1)) != int(start_ms) or int(manifest.get("period_end_ms", -1)) != int(end_ms):
            raise ValueError(
                "Replay period does not match snapshot. "
                f"snapshot={manifest.get('period_start_ms')}..{manifest.get('period_end_ms')} "
                f"requested={start_ms}..{end_ms}"
            )
        files = manifest.get("history_files")
        if not isinstance(files, dict) or not files:
            raise ValueError("Backtest snapshot contains no history file manifest")
        symbols = [str(s).upper() for s in manifest.get("symbols", [])]
        expected_keys = [f"symbol:{symbol}" for symbol in symbols] + ["btc_context:BTC_USDT"]
        missing_keys = [key for key in expected_keys if key not in files]
        if missing_keys:
            raise ValueError("Backtest snapshot missing required history entries: " + ", ".join(missing_keys[:20]))
        expected_symbols_hash = _sha256(_canonical_json_bytes(symbols))
        if manifest.get("symbols_sha256") != expected_symbols_hash:
            raise ValueError("Backtest snapshot symbol-list checksum mismatch")
        # Verify every market-data file before any strategy evaluation begins.
        for key, item in files.items():
            rel = str(item.get("path") or "")
            file_path = snapshot_path / rel
            if not rel or not file_path.is_file():
                raise ValueError(f"Snapshot history missing for {key}: {rel}")
            digest = _sha256(file_path.read_bytes())
            if digest != item.get("sha256"):
                raise ValueError(f"Snapshot checksum mismatch for {key}: {rel}")
        store = cls(mode="replay", path=snapshot_path, manifest=manifest)
        current_source_sha, _ = _source_fingerprint(Path(__file__).resolve().parents[2])
        LOGGER.info(
            "BACKTEST REPLAY LOADED | path=%s symbols=%d history_files=%d "
            "period_start_ms=%d period_end_ms=%d source_sha256_snapshot=%s "
            "source_sha256_current=%s source_match=%s",
            snapshot_path,
            len(manifest.get("symbols", [])),
            len(files),
            int(start_ms),
            int(end_ms),
            str(manifest.get("source_sha256", "UNKNOWN")),
            current_source_sha,
            current_source_sha == manifest.get("source_sha256"),
        )
        return store

    @property
    def symbols(self) -> list[str]:
        return [str(s).upper() for s in self.manifest.get("symbols", [])]

    def set_symbols(self, symbols: list[str]) -> None:
        self._selected_symbols = [str(s).upper() for s in symbols]

    @staticmethod
    def _role_key(symbol: str, role: str) -> str:
        return f"{role}:{str(symbol).upper()}"

    def _filename(self, symbol: str, role: str) -> str:
        safe = re.sub(r"[^A-Z0-9_-]+", "_", str(symbol).upper())
        prefix = "btc_context" if role == "btc_context" else "symbol"
        return f"histories/{prefix}__{safe}.json.gz"

    def write_history(self, history: Any, *, role: str = "symbol") -> None:
        if self.mode != "capture":
            return
        rows = {
            "schema_version": SCHEMA_VERSION,
            "symbol": str(history.symbol).upper(),
            "role": role,
            "candles_1d": [_candle_row(row) for row in history.candles_1d],
            "candles_12h": [_candle_row(row) for row in history.candles_12h],
            "candles_4h": [_candle_row(row) for row in history.candles_4h],
            "candles_1h": [_candle_row(row) for row in history.candles_1h],
        }
        raw = _canonical_json_bytes(rows)
        packed = gzip.compress(raw, mtime=0)
        relative = self._filename(history.symbol, role)
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(packed)
        tmp.replace(target)
        key = self._role_key(history.symbol, role)
        self._files[key] = {
            "path": relative,
            "sha256": _sha256(packed),
            "bytes": len(packed),
            "rows": {
                "1d": len(rows["candles_1d"]),
                "12h": len(rows["candles_12h"]),
                "4h": len(rows["candles_4h"]),
                "1h": len(rows["candles_1h"]),
            },
        }

    def load_history(self, symbol: str, *, role: str = "symbol") -> Any:
        if self.mode != "replay":
            raise RuntimeError("load_history requires replay mode")
        key = self._role_key(symbol, role)
        item = self.manifest.get("history_files", {}).get(key)
        if not isinstance(item, Mapping):
            raise ValueError(f"History is not present in frozen snapshot: {key}")
        path = self.path / str(item["path"])
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Could not load frozen history {key}: {exc}") from exc
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported history schema for {key}")
        if str(payload.get("symbol", "")).upper() != str(symbol).upper() or payload.get("role") != role:
            raise ValueError(f"Frozen history identity mismatch for {key}")
        # Import lazily to avoid the runner/store module cycle.
        from .runner import SymbolHistory

        # convert_candles applies the exact same canonical candle validation used by the engine.
        return SymbolHistory(
            symbol=str(symbol).upper(),
            candles_1d=list(convert_candles(payload.get("candles_1d", []))),
            candles_12h=list(convert_candles(payload.get("candles_12h", []))),
            candles_4h=list(convert_candles(payload.get("candles_4h", []))),
            candles_1h=list(convert_candles(payload.get("candles_1h", []))),
        )

    def finalize(
        self,
        *,
        start_ms: int,
        end_ms: int,
        symbols: list[str],
        engine_config: Mapping[str, Any],
        settings: Any,
        tp_mode: str,
        project_root: Path,
        runner_max_concurrency: int = 1,
    ) -> dict[str, Any]:
        if self.mode != "capture":
            return self.manifest
        source_sha, source_files = _source_fingerprint(project_root)
        expected_keys = [self._role_key(symbol, "symbol") for symbol in symbols]
        expected_keys.append(self._role_key("BTC_USDT", "btc_context"))
        missing = sorted(key for key in expected_keys if key not in self._files)
        complete = not missing
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "complete": complete,
            "snapshot_path_hint": str(self.path),
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "period_start_ms": int(start_ms),
            "period_end_ms": int(end_ms),
            "symbols": [str(s).upper() for s in symbols],
            "symbols_sha256": _sha256(_canonical_json_bytes([str(s).upper() for s in symbols])),
            "history_files": dict(sorted(self._files.items())),
            "missing_history_keys": missing,
            "engine_config": _safe_value(engine_config),
            "engine_config_fingerprint": str(engine_config.get("fingerprint", "")),
            "source_sha256": source_sha,
            "source_file_sha256": source_files,
            "capture_tp_mode": str(tp_mode),
            "runner_max_concurrency": int(runner_max_concurrency),
            "runtime": _runtime_info(),
            "settings": _settings_snapshot(settings),
            "note": "Snapshot contains normalized 1D/12H/4H/1H candles. Replay reuses stored 12H bars and does not fetch exchange data.",
        }
        target = self.path / "manifest.json"
        temp = target.with_suffix(".json.tmp")
        temp.write_bytes(_canonical_json_bytes(manifest))
        temp.replace(target)
        self.manifest = manifest
        LOGGER.info(
            "BACKTEST SNAPSHOT %s | path=%s symbols=%d history_files=%d missing=%d "
            "manifest_sha256=%s source_sha256=%s",
            "COMPLETE" if complete else "INCOMPLETE",
            self.path,
            len(symbols),
            len(self._files),
            len(missing),
            _sha256(target.read_bytes()),
            source_sha,
        )
        if missing:
            LOGGER.error("BACKTEST SNAPSHOT MISSING HISTORIES | keys=%s", ",".join(missing[:50]))
        return manifest

    def write_run_artifacts(
        self,
        *,
        summary: Any,
        trades: list[Any],
        symbols: list[str],
        start_ms: int,
        end_ms: int,
        tp_mode: str,
        engine_config: Mapping[str, Any],
        settings: Any,
        project_root: Path,
        runner_max_concurrency: int = 1,
    ) -> dict[str, str]:
        """Write exact trade ledger and an experiment manifest for run comparison."""
        artifact_root = Path(os.getenv("V11_BACKTEST_ARTIFACT_DIR", str(self.path / "results"))).expanduser().resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex[:12]
        ordered = sorted(trades, key=lambda t: (int(t.signal_time_ms), str(t.symbol), str(t.side)))
        ledger = [_safe_value(t) for t in ordered]
        ledger_bytes = _canonical_json_bytes(ledger)
        ledger_sha = _sha256(ledger_bytes)
        ledger_path = artifact_root / f"trades_{str(tp_mode).upper().replace('.', 'p')}_{run_id}.json"
        ledger_path.write_bytes(json.dumps(ledger, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        source_sha, source_files = _source_fingerprint(project_root)
        symbols_list = [str(s).upper() for s in symbols]
        summary_payload = _safe_value(summary)
        manifest_path = self.path / "manifest.json"
        snapshot_manifest_sha = _sha256(manifest_path.read_bytes()) if manifest_path.is_file() else None
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "period_start_ms": int(start_ms),
            "period_end_ms": int(end_ms),
            "window_is_fixed": True,
            "tp_mode": str(tp_mode),
            "symbols": symbols_list,
            "symbols_sha256": _sha256(_canonical_json_bytes(symbols_list)),
            "trade_count": len(ledger),
            "trade_ledger_sha256": ledger_sha,
            "trade_ledger_file": ledger_path.name,
            "source_sha256": source_sha,
            "source_file_sha256": source_files,
            "engine_config": _safe_value(engine_config),
            "engine_config_fingerprint": str(engine_config.get("fingerprint", "")),
            "settings": _settings_snapshot(settings),
            "runner_max_concurrency": int(runner_max_concurrency),
            "runtime": _runtime_info(),
            "replay_snapshot": str(self.path),
            "snapshot_manifest_sha256": snapshot_manifest_sha,
            "summary": summary_payload,
        }
        record_bytes = json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
        record_path = artifact_root / f"run_{str(tp_mode).upper().replace('.', 'p')}_{run_id}.json"
        record_path.write_bytes(record_bytes)
        LOGGER.info(
            "BACKTEST REPRO ARTIFACTS | run_id=%s tp_mode=%s path=%s "
            "trades=%d ledger_sha256=%s source_sha256=%s symbols_sha256=%s",
            run_id, tp_mode, artifact_root, len(ledger), ledger_sha, source_sha, record["symbols_sha256"],
        )
        return {"ledger": str(ledger_path), "run_manifest": str(record_path), "ledger_sha256": ledger_sha, "source_sha256": source_sha}
