from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from rslr.indicators import atr, donchian


@dataclass(frozen=True)
class EntryCandidate:
    side: Literal["LONG", "SHORT"]
    module: Literal["SWEEP", "DONCHIAN"]
    entry: float
    stop: float
    signal_timestamp: pd.Timestamp


def sweep_reclaim(frame: pd.DataFrame, lookback: int = 20, atr_period: int = 14) -> EntryCandidate | None:
    if len(frame) < max(lookback, atr_period) + 2:
        return None
    current = frame.iloc[-1]
    previous = frame.iloc[-lookback - 1 : -1]
    level_low = float(previous.low.min())
    level_high = float(previous.high.max())
    current_atr = float(atr(frame, atr_period).iloc[-1])
    if not pd.notna(current_atr) or current_atr <= 0:
        return None
    timestamp = pd.Timestamp(current.timestamp)
    if current.low < level_low and current.close > level_low and current.close > current.open:
        stop = min(float(current.low), float(previous.low.tail(5).min())) - 0.15 * current_atr
        if stop < float(current.close):
            return EntryCandidate("LONG", "SWEEP", float(current.close), stop, timestamp)
    if current.high > level_high and current.close < level_high and current.close < current.open:
        stop = max(float(current.high), float(previous.high.tail(5).max())) + 0.15 * current_atr
        if stop > float(current.close):
            return EntryCandidate("SHORT", "SWEEP", float(current.close), stop, timestamp)
    return None


def donchian_breakout(frame: pd.DataFrame, lookback: int = 20, atr_period: int = 14) -> EntryCandidate | None:
    if len(frame) < max(lookback, atr_period) + 2:
        return None
    current = frame.iloc[-1]
    upper, lower = donchian(frame, lookback)
    upper_level = upper.iloc[-1]
    lower_level = lower.iloc[-1]
    current_atr = float(atr(frame, atr_period).iloc[-1])
    if pd.isna(upper_level) or pd.isna(lower_level) or not pd.notna(current_atr) or current_atr <= 0:
        return None
    timestamp = pd.Timestamp(current.timestamp)
    if current.close > upper_level and current.close > current.open:
        structure_low = float(frame.low.iloc[-6:-1].min())
        stop = min(structure_low, float(current.close) - current_atr) - 0.1 * current_atr
        if stop < float(current.close):
            return EntryCandidate("LONG", "DONCHIAN", float(current.close), stop, timestamp)
    if current.close < lower_level and current.close < current.open:
        structure_high = float(frame.high.iloc[-6:-1].max())
        stop = max(structure_high, float(current.close) + current_atr) + 0.1 * current_atr
        if stop > float(current.close):
            return EntryCandidate("SHORT", "DONCHIAN", float(current.close), stop, timestamp)
    return None
