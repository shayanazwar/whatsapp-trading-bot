"""Non-destructive public-data alignment probe. Uses no credentials and never submits orders."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.config import load_settings
from app.market.candles import Candle, INTERVAL_SECONDS, aggregate_12h_from_4h, validate_candle
from app.mexc.client import MexcClient
from app.symbols import normalize_symbol


def _aggregate_from_1h(bars: Sequence[Candle], target_tf: str) -> list[Candle]:
    """Probe-only arithmetic to compare 1H bars to native 4H/1D candles.

    These results validate data consistency only; they are not a hidden strategy timeframe.
    """
    if target_tf not in {"4H", "1D", "12H"}:
        raise ValueError("Unsupported probe aggregation target")
    size = {"4H": 4, "12H": 12, "1D": 24}[target_tf]
    duration = INTERVAL_SECONDS[target_tf]
    ordered = sorted((c for c in bars if c.timeframe == "1H" and c.complete), key=lambda c: c.open_time)
    by_time: dict[int, Candle] = {c.open_time: c for c in ordered}
    output: list[Candle] = []
    for start in sorted(by_time):
        if start % duration != 0:
            continue
        chunk = [by_time.get(start + i * 3600) for i in range(size)]
        if any(c is None for c in chunk):
            continue
        xs = [c for c in chunk if c]
        if len(xs) != size:
            continue
        if any(not c.complete for c in xs):
            continue
        amount = None if any(c.amount is None for c in xs) else sum(c.amount or 0 for c in xs)
        candle = Candle(
            symbol=xs[0].symbol, timeframe=target_tf, open_time=start,
            open=xs[0].open, high=max(c.high for c in xs), low=min(c.low for c in xs), close=xs[-1].close,
            volume=sum(c.volume for c in xs), amount=amount, complete=True, source=f"probe_aggregate_{target_tf}_from_1h",
        )
        validate_candle(candle)
        output.append(candle)
    return output


def _compare_series(native: Sequence[Candle], aggregated: Sequence[Candle], tolerance: float = 1e-7) -> dict[str, Any]:
    agg_by_time = {c.open_time: c for c in aggregated}
    comparisons: list[dict[str, Any]] = []
    field_diffs: Counter[str] = Counter()
    common = 0
    for ref in native:
        other = agg_by_time.get(ref.open_time)
        if not other:
            continue
        common += 1
        differences = {}
        for field in ("open", "high", "low", "close", "volume"):
            a, b = float(getattr(ref, field)), float(getattr(other, field))
            scale = max(abs(a), abs(b), 1.0)
            if abs(a - b) > tolerance * scale:
                differences[field] = {"native": a, "aggregated": b, "relative_difference": abs(a - b) / scale}
                field_diffs[field] += 1
        if differences and len(comparisons) < 25:
            comparisons.append({"open_time": ref.open_time, "differences": differences})
    return {"native_count": len(native), "aggregated_count": len(aggregated), "overlap_count": common,
            "field_mismatch_counts": dict(field_diffs), "sample_mismatches": comparisons,
            "pass": common > 0 and not field_diffs}


async def run_probe(output_path: Path, symbols: Sequence[str] | None = None) -> dict[str, Any]:
    settings = load_settings()
    symbols = [normalize_symbol(s) for s in (symbols or ["BTC_USDT", "ETH_USDT", "SOL_USDT"])]
    now_local = int(time.time())
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": settings.mexc_rest_base_url,
        "symbols_requested": symbols,
        "credentials_used": False,
        "orders_submitted": 0,
        "strategy_timeframes": ["1D", "12H", "4H", "1H"],
        "status": "RUNNING",
        "alignment_gates_passed": False,
        "price_series_semantics_reviewed": False,
        "claims": {
            "native_12h_interval": "not used; 12H derived from completed 4H bars",
            "utc_alignment": "unresolved until observed timestamps pass the checks below",
            "realOpen_semantics": "unresolved; field presence alone cannot establish meaning",
        },
        "symbols": {},
        "warnings": [],
    }
    try:
        # The probe is diagnostic, not a production fetcher: one short attempt prevents an
        # unreachable public endpoint from hanging the research/CI job for minutes.
        async with MexcClient(settings.mexc_rest_base_url, min(settings.http_timeout_seconds, 5.0), 0) as client:
            try:
                server_time = await client.server_time()
                report["server_time_probe"] = {"status": "ok", "timestamp": server_time,
                                                "source": getattr(client, "server_time_source", "unreported")}
            except Exception as exc:
                report["server_time_probe"] = {"status": "unresolved", "error_type": type(exc).__name__}
                report["warnings"].append("Server time could not be verified from the returned payload; local clock used only to filter likely closed bars.")
            end = now_local
            for symbol in symbols:
                item: dict[str, Any] = {"symbol": symbol, "status": "running", "warnings": []}
                try:
                    one_hour, four_hour, daily = await asyncio.gather(
                        client.get_klines_window(symbol, "1H", start=end - 12 * 86400, end=end, now_epoch=end,
                            close_grace_seconds=settings.candle_close_grace_seconds, max_bars_per_request=1500),
                        client.get_klines_window(symbol, "4H", start=end - 30 * 86400, end=end, now_epoch=end,
                            close_grace_seconds=settings.candle_close_grace_seconds, max_bars_per_request=1000),
                        client.get_klines_window(symbol, "1D", start=end - 60 * 86400, end=end, now_epoch=end,
                            close_grace_seconds=settings.candle_close_grace_seconds, max_bars_per_request=200),
                    )
                    item["counts"] = {"1H": len(one_hour), "4H": len(four_hour), "1D": len(daily)}
                    item["first_open_times"] = {tf: bars[0].open_time if bars else None for tf, bars in (("1H", one_hour), ("4H", four_hour), ("1D", daily))}
                    item["last_close_times"] = {tf: bars[-1].close_time if bars else None for tf, bars in (("1H", one_hour), ("4H", four_hour), ("1D", daily))}
                    item["utc_boundary_counts"] = {
                        "1H_on_hour": sum(c.open_time % 3600 == 0 for c in one_hour),
                        "4H_on_expected_boundary": sum(c.open_time % 14400 == 0 for c in four_hour),
                        "1D_on_utc_midnight": sum(c.open_time % 86400 == 0 for c in daily),
                    }
                    item["gap_counts"] = {
                        "1H": sum(1 for a, b in zip(one_hour, one_hour[1:]) if b.open_time - a.open_time != 3600),
                        "4H": sum(1 for a, b in zip(four_hour, four_hour[1:]) if b.open_time - a.open_time != 14400),
                        "1D": sum(1 for a, b in zip(daily, daily[1:]) if b.open_time - a.open_time != 86400),
                    }
                    item["aggregation_comparisons"] = {
                        "4H_from_1H": _compare_series(four_hour, _aggregate_from_1h(one_hour, "4H")),
                        "1D_from_1H": _compare_series(daily, _aggregate_from_1h(one_hour, "1D")),
                        "12H_from_4H_count": len(aggregate_12h_from_4h(four_hour)),
                        "12H_from_1H_count_probe_only": len(_aggregate_from_1h(one_hour, "12H")),
                    }
                    real_fields = ["real_open", "real_high", "real_low", "real_close"]
                    item["real_field_presence"] = {field: sum(getattr(c, field) is not None for c in one_hour) for field in real_fields}
                    try:
                        contract = await client.contract_detail(symbol)
                        item["contract"] = None if contract is None else {
                            k: contract.get(k) for k in ("symbol", "state", "apiAllowed", "contractSize", "priceUnit", "volUnit", "minVol", "maxVol", "makerFeeRate", "takerFeeRate", "collectCycle") if k in contract
                        }
                    except Exception as exc:
                        item["contract_error_type"] = type(exc).__name__
                        item["warnings"].append("Contract metadata fetch failed")
                    try:
                        ticker = await client.ticker(symbol)
                        item["ticker_fields_present"] = [k for k in ("lastPrice", "fairPrice", "indexPrice", "bid1", "ask1", "timestamp") if k in ticker]
                    except Exception as exc:
                        item["ticker_error_type"] = type(exc).__name__
                    try:
                        funding = await client.funding_current(symbol)
                        item["funding_fields_present"] = [k for k in ("fundingRate", "collectCycle", "nextSettleTime", "timestamp", "symbol") if k in funding]
                        item["funding_current_sample"] = {k: funding.get(k) for k in item["funding_fields_present"]}
                    except Exception as exc:
                        item["funding_error_type"] = type(exc).__name__
                    if not four_hour or not daily or not one_hour:
                        item["warnings"].append("One or more requested candle series was empty.")
                    if item["utc_boundary_counts"]["4H_on_expected_boundary"] != len(four_hour):
                        item["warnings"].append("Some 4H timestamps do not match assumed UTC 4H boundaries; do not accept 12H construction until resolved.")
                    if item["utc_boundary_counts"]["1D_on_utc_midnight"] != len(daily):
                        item["warnings"].append("Some 1D timestamps do not align to UTC midnight; daily aggregation assumptions require review.")
                    comparisons = item["aggregation_comparisons"]
                    field_presence_ok = all(count == len(one_hour) for count in item["real_field_presence"].values())
                    item["alignment_checks"] = {
                        "1H_hour_boundaries": item["utc_boundary_counts"]["1H_on_hour"] == len(one_hour),
                        "4H_utc_boundaries": item["utc_boundary_counts"]["4H_on_expected_boundary"] == len(four_hour),
                        "1D_utc_midnight": item["utc_boundary_counts"]["1D_on_utc_midnight"] == len(daily),
                        "no_gaps_in_probe_window": all(v == 0 for v in item["gap_counts"].values()),
                        "4H_matches_1H_aggregation": bool(comparisons["4H_from_1H"]["pass"]),
                        "1D_matches_1H_aggregation": bool(comparisons["1D_from_1H"]["pass"]),
                        "real_ohlc_fields_present": field_presence_ok,
                        "contract_metadata_observed": item.get("contract") is not None,
                    }
                    item["alignment_gates_passed"] = bool(
                        one_hour and four_hour and daily and all(item["alignment_checks"].values())
                    )
                    item["status"] = "observed" if one_hour and four_hour and daily else "incomplete_data"
                except Exception as exc:
                    item["status"] = "failed"
                    item["error_type"] = type(exc).__name__
                    item["error"] = str(exc)[:300]
                    report["warnings"].append(f"{symbol} public-data probe failed; no empirical conclusion for that symbol.")
                report["symbols"][symbol] = item
            good = [v for v in report["symbols"].values() if v["status"] == "observed"]
            report["status"] = "COMPLETE_WITH_OBSERVATIONS" if good else "FAILED_NO_DATA"
            report["alignment_gates_passed"] = bool(
                good and set(symbols) == {s for s, item in report["symbols"].items()
                                           if item.get("status") == "observed" and item.get("alignment_gates_passed") is True}
            )
            if not report["alignment_gates_passed"]:
                report["warnings"].append(
                    "One or more deterministic alignment checks did not pass. A human must review the probe output."
                )
            if not good:
                report["warnings"].append("No symbol yielded a complete dataset; do not infer outage cause or candle semantics.")
    except Exception as exc:
        report["status"] = "FAILED_BEFORE_DATA"
        report["fatal_error_type"] = type(exc).__name__
        report["fatal_error"] = str(exc)[:300]
        report["warnings"].append("Probe could not establish public MEXC connectivity; no data-derived conclusion was reached.")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="data/alignment_probe.json")
    parser.add_argument("--symbols", default="BTC_USDT,ETH_USDT,SOL_USDT")
    args = parser.parse_args()
    report = asyncio.run(run_probe(Path(args.output), [x.strip() for x in args.symbols.split(",") if x.strip()]))
    print(json.dumps({"status": report["status"], "output": str(Path(args.output)), "symbols": list(report.get("symbols", {})), "warnings": report.get("warnings", [])}, indent=2))
    return 0 if report["status"] == "COMPLETE_WITH_OBSERVATIONS" else 2


if __name__ == "__main__":
    sys.exit(main())
