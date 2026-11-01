"""Research candidate: 15M trend context + 3M pullback/reclaim.

This is a hypothesis, not a claim of profitability. All decisions use completed
3M bars and 15M bars aggregated only from completed 3M inputs.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from app.market.candles import Candle, aggregate_15m_from_3m
from app.market.indicators import atr, ema, rsi


@dataclass(frozen=True, slots=True)
class MtfTprConfig:
    ema_fast: int = 21
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    volume_period: int = 20
    min_relative_volume: float = 1.5
    atr_percentile_lookback: int = 480
    htf_warmup_bars: int = 250
    atr_percentile_low: float = 20.0
    atr_percentile_high: float = 80.0
    pullback_min_atr: float = 0.0
    pullback_max_atr: float = 0.8
    structure_lookback: int = 50
    structure_clearance_atr: float = 0.25
    long_rsi_ceiling: float = 75.0
    short_rsi_floor: float = 25.0
    min_score: float = 65.0
    grade_a_score: float = 80.0
    stop_atr_multiple: float = 1.5
    target_atr_multiple: float = 2.5

    def validate(self) -> None:
        if self.ema_fast < 2 or self.ema_slow <= self.ema_fast:
            raise ValueError("MTF EMA periods invalid")
        if not 50 <= self.htf_warmup_bars <= 1000:
            raise ValueError("HTF warm-up bars must be between 50 and 1000")
        if self.rsi_period < 2 or self.atr_period < 2 or self.volume_period < 2:
            raise ValueError("MTF indicator periods must be >= 2")
        if self.min_relative_volume <= 0 or self.pullback_max_atr <= self.pullback_min_atr:
            raise ValueError("MTF volume/pullback settings invalid")
        if not 0 <= self.atr_percentile_low < self.atr_percentile_high <= 100:
            raise ValueError("ATR percentile bounds invalid")
        if not 0 <= self.min_score <= self.grade_a_score <= 100:
            raise ValueError("Signal score thresholds invalid")
        if self.stop_atr_multiple <= 0 or self.target_atr_multiple <= 0:
            raise ValueError("ATR stop/target multiples must be positive")


@dataclass(frozen=True, slots=True)
class MtfTprSignal:
    symbol: str
    timestamp: int
    candle_index: int
    direction: str
    reason: tuple[str, ...]
    close: float
    rsi: float
    ema21: float
    ema50: float
    ema100: float
    atr: float
    stop_price: float
    target_price: float
    score: float
    grade: str
    relative_volume: float
    volume_basis: str
    htf_ema_fast: float
    htf_ema_slow: float
    planned_price_rr: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _percentile_sorted(ordered: list[float], percentile: float) -> float:
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def evaluate_mtf_tpr(candles: Sequence[Candle], config: MtfTprConfig | None = None) -> list[MtfTprSignal]:
    cfg = config or MtfTprConfig()
    cfg.validate()
    bars = sorted((c for c in candles if c.complete and c.timeframe == "3M"), key=lambda c: c.open_time)
    if not bars:
        return []
    if len({c.symbol for c in bars}) != 1 or len({c.open_time for c in bars}) != len(bars):
        raise ValueError("MTF input must contain unique 3M candles for one symbol")
    htf = aggregate_15m_from_3m(bars)
    if len(htf) < cfg.htf_warmup_bars + 2:
        return []

    closes = [c.close for c in bars]
    efast = ema(closes, cfg.ema_fast)
    eslow = ema(closes, cfg.ema_slow)
    eslow_report = ema(closes, 100)
    rsi_values = rsi(closes, cfg.rsi_period)
    atr_values = atr([c.high for c in bars], [c.low for c in bars], closes, cfg.atr_period)
    htf_fast = ema([c.close for c in htf], cfg.ema_fast)
    htf_slow = ema([c.close for c in htf], cfg.ema_slow)
    htf_closes = [c.close_time for c in htf]
    signals: list[MtfTprSignal] = []
    warmup = max(cfg.ema_slow + 2, cfg.rsi_period + 2, cfg.atr_period + 2, cfg.volume_period + 2)
    # Do not let an EMA/ATR chain bridge missing source data. A fresh contiguous
    # history window is required after any 3M or 15M gap.
    bar_run = [1] * len(bars)
    for j in range(1, len(bars)):
        bar_run[j] = bar_run[j - 1] + 1 if bars[j].open_time - bars[j - 1].open_time == 180 else 1
    htf_run = [1] * len(htf)
    for j in range(1, len(htf)):
        htf_run[j] = htf_run[j - 1] + 1 if htf[j].open_time - htf[j - 1].open_time == 900 else 1
    atr_ordered: list[float] = []
    atr_window: deque[float] = deque()

    for i in range(warmup, len(bars)):
        bar = bars[i]
        av = atr_values[i]
        rv = rsi_values[i]
        ef, es = efast[i], eslow[i]
        if None in (av, rv, ef, es) or float(av) <= 0:
            continue
        av_float = float(av)
        insort(atr_ordered, av_float)
        atr_window.append(av_float)
        if len(atr_window) > cfg.atr_percentile_lookback:
            expired_atr = atr_window.popleft()
            expired_index = bisect_left(atr_ordered, expired_atr)
            if expired_index < len(atr_ordered):
                atr_ordered.pop(expired_index)
        if bar_run[i] < cfg.atr_percentile_lookback:
            continue
        # 15M context is the most recently completed 15M bar at/before this 3M close.
        htf_i = bisect_right(htf_closes, bar.close_time) - 1
        if (htf_i < cfg.htf_warmup_bars or htf_run[htf_i] < cfg.htf_warmup_bars + 1
                or htf_fast[htf_i] is None or htf_slow[htf_i] is None):
            continue
        fast_htf, slow_htf = float(htf_fast[htf_i]), float(htf_slow[htf_i])
        if len(atr_ordered) < min(cfg.atr_period * 3, 42):
            continue
        atr_low = _percentile_sorted(atr_ordered, cfg.atr_percentile_low)
        atr_high = _percentile_sorted(atr_ordered, cfg.atr_percentile_high)
        if not atr_low <= av_float <= atr_high:
            continue

        volume_window = bars[i-cfg.volume_period:i]
        quote_volume_available = bar.amount is not None and bar.amount > 0 and all(
            c.amount is not None and c.amount > 0 for c in volume_window
        )
        if quote_volume_available:
            volume_values = [float(c.amount) for c in volume_window if c.amount is not None]
            current_volume = float(bar.amount)
        else:
            volume_values = [float(c.volume) for c in volume_window]
            current_volume = float(bar.volume)
        baseline = sorted(volume_values)[len(volume_values)//2] if volume_values else 0.0
        relative_volume = current_volume / baseline if baseline > 0 else 0.0
        if relative_volume < cfg.min_relative_volume:
            continue

        pullback_bars = bars[max(0, i-3):i]
        if len(pullback_bars) < 2:
            continue
        long_trend = float(ef) > float(es) and fast_htf > slow_htf and bar.close > float(ef)
        short_trend = float(ef) < float(es) and fast_htf < slow_htf and bar.close < float(ef)
        pullback_distance = abs(bar.close - float(ef)) / float(av)
        if not cfg.pullback_min_atr <= pullback_distance <= cfg.pullback_max_atr:
            continue
        prior_pullback_long = min(c.low for c in pullback_bars) <= float(ef) + cfg.pullback_max_atr * float(av)
        prior_pullback_short = max(c.high for c in pullback_bars) >= float(ef) - cfg.pullback_max_atr * float(av)
        candle_range = max(bar.high - bar.low, 1e-12)
        close_location = (bar.close - bar.low) / candle_range
        prior_high = max(c.high for c in bars[max(0, i-cfg.structure_lookback):i])
        prior_low = min(c.low for c in bars[max(0, i-cfg.structure_lookback):i])
        rsi_value = float(rv)

        direction: str | None = None
        structure_room_atr = 0.0
        acceptance = 0.0
        pullback_score = 0.0
        reasons: list[str] = []
        if long_trend and prior_pullback_long and close_location >= 0.70 and rsi_value < cfg.long_rsi_ceiling:
            structure_room_atr = (prior_high - bar.close) / float(av)
            if prior_high <= bar.close or structure_room_atr >= cfg.structure_clearance_atr:
                direction = "LONG"
                acceptance = min(1.0, max(0.0, (close_location - 0.70) / 0.30))
                pullback_score = max(0.0, 1.0 - abs(pullback_distance - 0.45) / 0.8)
                reasons.extend(["15M_BULLISH_CONTEXT", "3M_BULLISH_REGIME", "EMA21_PULLBACK_RECLAIM", "QUOTE_RVOL_OK"])
        elif short_trend and prior_pullback_short and close_location <= 0.30 and rsi_value > cfg.short_rsi_floor:
            structure_room_atr = (bar.close - prior_low) / float(av)
            if prior_low >= bar.close or structure_room_atr >= cfg.structure_clearance_atr:
                direction = "SHORT"
                acceptance = min(1.0, max(0.0, (0.30 - close_location) / 0.30))
                pullback_score = max(0.0, 1.0 - abs(pullback_distance - 0.45) / 0.8)
                reasons.extend(["15M_BEARISH_CONTEXT", "3M_BEARISH_REGIME", "EMA21_PULLBACK_REJECT", "QUOTE_RVOL_OK"])
        if not direction:
            continue

        volume_score = min(1.0, max(0.0, (relative_volume - cfg.min_relative_volume) / max(cfg.min_relative_volume, 1e-9) + 0.5))
        structure_score = min(1.0, max(0.0, structure_room_atr / 1.0))
        score = 30.0 * pullback_score + 25.0 * volume_score + 20.0 * acceptance + 15.0 + 10.0 * structure_score
        if score < cfg.min_score:
            continue
        if direction == "LONG":
            swing_stop = min(c.low for c in bars[max(0, i-10):i+1])
            stop = min(swing_stop - 0.1 * float(av), bar.close - cfg.stop_atr_multiple * float(av))
            target = bar.close + cfg.target_atr_multiple * float(av)
        else:
            swing_stop = max(c.high for c in bars[max(0, i-10):i+1])
            stop = max(swing_stop + 0.1 * float(av), bar.close + cfg.stop_atr_multiple * float(av))
            target = bar.close - cfg.target_atr_multiple * float(av)
        grade = "A" if score >= cfg.grade_a_score else "B"
        signals.append(MtfTprSignal(
            symbol=bar.symbol, timestamp=bar.close_time, candle_index=i, direction=direction,
            reason=tuple(reasons), close=bar.close, rsi=rsi_value, ema21=float(ef),
            ema50=float(es), ema100=float(eslow_report[i]) if eslow_report[i] is not None else float(es), atr=float(av), stop_price=stop,
            target_price=target, score=round(score, 2), grade=grade,
            relative_volume=round(relative_volume, 4),
            volume_basis="quote_amount" if quote_volume_available else "base_volume_within_symbol_fallback",
            htf_ema_fast=fast_htf, htf_ema_slow=slow_htf,
            planned_price_rr=round(abs(target - bar.close) / max(abs(bar.close - stop), 1e-12), 4),
        ))
    return signals
