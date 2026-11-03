from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from app.strategy.indicators import enrich, structure_state


@dataclass
class StrategyDecision:
    symbol: str
    decision: str
    side: str | None
    score: float
    entry: float | None
    stop: float | None
    tp1: float | None
    tp2: float | None
    reward_risk: float | None
    regime_1d: str
    direction_12h: str
    structure_4h: dict
    setup_4h: str | None
    trigger_1h: str | None
    reasons: list[str]
    metrics: dict

    def as_dict(self):
        return asdict(self)


def _trend(frame: pd.DataFrame) -> str:
    if len(frame) < 55:
        return "UNKNOWN"
    x = enrich(frame)
    last = x.iloc[-1]
    if pd.isna(last["ema50"]):
        return "UNKNOWN"
    if last["ema20"] > last["ema50"] and last["close"] > last["ema20"]:
        return "BULLISH"
    if last["ema20"] < last["ema50"] and last["close"] < last["ema20"]:
        return "BEARISH"
    return "SIDEWAYS"


def _setup_4h(frame: pd.DataFrame, direction: str) -> str | None:
    if len(frame) < 30 or direction not in {"BULLISH", "BEARISH"}:
        return None
    x = enrich(frame)
    last = x.iloc[-1]
    prior = x.iloc[-21:-1]
    if pd.isna(last["ema20"]) or pd.isna(last["atr14"]):
        return None
    if direction == "BULLISH":
        pullback = last["low"] <= last["ema20"] * 1.012 and last["close"] > last["ema20"]
        sweep = last["low"] < prior["low"].min() and last["close"] > prior["low"].min()
        breakout_retest = last["high"] > prior["high"].max() and last["low"] <= prior["high"].max() * 1.01 and last["close"] > prior["high"].max()
    else:
        pullback = last["high"] >= last["ema20"] * 0.988 and last["close"] < last["ema20"]
        sweep = last["high"] > prior["high"].max() and last["close"] < prior["high"].max()
        breakout_retest = last["low"] < prior["low"].min() and last["high"] >= prior["low"].min() * 0.99 and last["close"] < prior["low"].min()
    if sweep:
        return "LIQUIDITY_SWEEP_RECLAIM"
    if breakout_retest:
        return "BREAKOUT_RETEST"
    if pullback:
        return "TREND_PULLBACK_VALUE_REENTRY"
    return None


def _trigger_1h(frame: pd.DataFrame, side: str) -> str | None:
    if len(frame) < 25 or side not in {"LONG", "SHORT"}:
        return None
    x = enrich(frame)
    last, previous = x.iloc[-1], x.iloc[-2]
    if pd.isna(last["atr14"]) or last["atr14"] <= 0:
        return None
    rel_volume = float(last["volume"] / last["volume_sma20"]) if last["volume_sma20"] and not pd.isna(last["volume_sma20"]) else 1.0
    body_ok = float(last["body_atr"] or 0) >= 0.20
    if side == "LONG":
        if last["close"] > previous["high"] and last["close"] > last["open"] and body_ok and rel_volume >= 0.75:
            return "BULLISH_CLOSE_ABOVE_PRIOR_HIGH"
    else:
        if last["close"] < previous["low"] and last["close"] < last["open"] and body_ok and rel_volume >= 0.75:
            return "BEARISH_CLOSE_BELOW_PRIOR_LOW"
    return None


def analyze_multitimeframe(symbol: str, d1: pd.DataFrame, h12: pd.DataFrame,
                           h4: pd.DataFrame, h1: pd.DataFrame) -> StrategyDecision:
    """Transparent completed-candle strategy; no model-generated entry prices."""
    for name, frame in [("1D", d1), ("12H", h12), ("4H", h4), ("1H", h1)]:
        if frame is None or len(frame) < 55:
            return StrategyDecision(symbol, "INSUFFICIENT_DATA", None, 0, None, None, None, None, None,
                                    "UNKNOWN", "UNKNOWN", {}, None, None,
                                    [f"Insufficient completed {name} candles; need at least 55"], {})
    x1d, x12, x4, x1 = (enrich(x) for x in (d1, h12, h4, h1))
    regime = _trend(d1)
    direction = _trend(h12)
    struct = structure_state(h4)
    side = "LONG" if regime == direction == "BULLISH" else "SHORT" if regime == direction == "BEARISH" else None
    reasons = []
    score = 0.0
    if side:
        score += 25
        reasons.append(f"1D regime and 12H direction align {side}")
    else:
        reasons.append("1D and 12H are not aligned; wait for confirmation")
    if struct["trend"] == ("BULLISH" if side == "LONG" else "BEARISH") and side:
        score += 15
        reasons.append("4H confirmed swing structure agrees")
    elif struct["bos"] == ("BULLISH" if side == "LONG" else "BEARISH") and side:
        score += 10
        reasons.append("4H structure shows a matching break of structure")
    setup = _setup_4h(h4, "BULLISH" if side == "LONG" else "BEARISH") if side else None
    if setup:
        score += 20
        reasons.append(f"4H setup: {setup}")
    trigger = _trigger_1h(h1, side) if side else None
    if trigger:
        score += 20
        reasons.append(f"1H trigger: {trigger}")
    last = x1.iloc[-1]
    if not pd.isna(last["rsi14"]):
        if side == "LONG" and 45 <= last["rsi14"] <= 72:
            score += 5
        elif side == "SHORT" and 28 <= last["rsi14"] <= 55:
            score += 5
    volume_ratio = float(last["volume"] / last["volume_sma20"]) if last["volume_sma20"] and not pd.isna(last["volume_sma20"]) else 1.0
    if volume_ratio >= 1.0:
        score += 5
    if not side or not setup or not trigger:
        decision = "WAIT" if side else "NO_TRADE"
        return StrategyDecision(symbol, decision, side, score, None, None, None, None, None,
                                regime, direction, struct, setup, trigger, reasons,
                                {"rsi14": float(last["rsi14"]), "atr14": float(last["atr14"]), "volume_ratio": volume_ratio})
    entry = float(last["close"])
    atr_value = float(last["atr14"])
    if not np.isfinite(atr_value) or atr_value <= 0:
        return StrategyDecision(symbol, "INSUFFICIENT_DATA", side, score, None, None, None, None, None,
                                regime, direction, struct, setup, trigger, ["ATR unavailable"], {})
    # Structural stop is outside the last confirmed 4H swing, with ATR buffer.
    if side == "LONG":
        structural = struct.get("swing_low")
        stop = min(float(structural), entry - 1.2 * atr_value) if structural else entry - 1.5 * atr_value
        if stop >= entry:
            stop = entry - 1.5 * atr_value
        risk = entry - stop
        tp1, tp2 = entry + 1.0 * risk, entry + 2.0 * risk
    else:
        structural = struct.get("swing_high")
        stop = max(float(structural), entry + 1.2 * atr_value) if structural else entry + 1.5 * atr_value
        if stop <= entry:
            stop = entry + 1.5 * atr_value
        risk = stop - entry
        tp1, tp2 = entry - 1.0 * risk, entry - 2.0 * risk
    rr = abs(tp2 - entry) / risk if risk else 0
    decision = "LONG_CANDIDATE" if side == "LONG" else "SHORT_CANDIDATE"
    if score < 70 or rr < 1.99:
        decision = "WAIT"
        reasons.append("Candidate did not meet the initial score / 2R research threshold")
    else:
        reasons.append("Completed-candle strategy validation passed; paper research only")
    return StrategyDecision(symbol, decision, side, score, entry, stop, tp1, tp2, rr,
                            regime, direction, struct, setup, trigger, reasons,
                            {"rsi14": float(last["rsi14"]), "atr14": atr_value,
                             "volume_ratio": volume_ratio, "ema20_1h": float(last["ema20"]),
                             "ema50_1h": float(last["ema50"])})
