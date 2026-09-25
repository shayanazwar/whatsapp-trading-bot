from __future__ import annotations

from typing import Any


def validate_analysis(
    data: dict[str, Any],
    *,
    min_confluence: int,
    min_rr: float,
    require_increasing_volume: bool = False,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    side = data.get("setup")

    if side not in {"LONG", "SHORT"}:
        reasons.append("No deterministic LONG/SHORT setup")
        return False, reasons

    if int(data.get("score", 0)) < min_confluence:
        reasons.append(f"Confluence {data.get('score')} < {min_confluence}")

    if float(data.get("rr") or 0) < min_rr:
        reasons.append(f"RR {float(data.get('rr') or 0):.2f} < {min_rr:.2f}")

    if require_increasing_volume and data.get("volume") != "INCREASING":
        reasons.append("Volume confirmation is not INCREASING")

    bullish = int(data.get("bullish_points", 0))
    bearish = int(data.get("bearish_points", 0))
    if side == "LONG" and not (bullish >= 4 and bearish == 0):
        reasons.append("LONG directional factors are not fully aligned")
    if side == "SHORT" and not (bearish >= 4 and bullish == 0):
        reasons.append("SHORT directional factors are not fully aligned")

    entry = float(data.get("entry") or 0)
    sl = float(data.get("stop_loss") or 0)
    tp1 = float(data.get("tp1") or 0)
    tp2 = float(data.get("tp2") or 0)
    support = data.get("support")
    resistance = data.get("resistance")

    if side == "LONG":
        if not (sl < entry < tp1 < tp2):
            reasons.append("LONG price ordering failed")
        if resistance is not None and entry >= float(resistance):
            reasons.append("LONG entry is at/above detected resistance")
    else:
        if not (tp2 < tp1 < entry < sl):
            reasons.append("SHORT price ordering failed")
        if support is not None and entry <= float(support):
            reasons.append("SHORT entry is at/below detected support")

    return not reasons, reasons
