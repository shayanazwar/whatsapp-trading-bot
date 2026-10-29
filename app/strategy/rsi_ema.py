"""Backtest-first 3-minute LONG strategy selected by the user.

The strategy is a testable hypothesis. It uses only completed bars and never places orders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

from app.market.candles import Candle
from app.market.indicators import ema, rsi


@dataclass(frozen=True, slots=True)
class RsiEmaConfig:
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    ema_fast: int = 21
    ema_mid: int = 50
    ema_slow: int = 100
    cross_lookback: int = 3
    volume_period: int = 20
    volume_multiplier: float = 1.2

    def validate(self) -> None:
        if self.rsi_period < 2 or min(self.ema_fast, self.ema_mid, self.ema_slow) < 2:
            raise ValueError("Indicator periods must be >= 2")
        if not 0 < self.rsi_overbought <= 100:
            raise ValueError("RSI threshold must be in (0, 100]")
        if self.ema_fast >= self.ema_mid or self.ema_mid >= self.ema_slow:
            raise ValueError("EMA periods must satisfy fast < mid < slow")
        if self.cross_lookback < 1 or self.volume_period < 2 or self.volume_multiplier <= 0:
            raise ValueError("Invalid crossover or volume settings")


@dataclass(frozen=True, slots=True)
class StrategySignal:
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

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_signals(candles: Sequence[Candle], config: RsiEmaConfig | None = None) -> list[StrategySignal]:
    """Return candidate signals at bar close; entries must occur no earlier than the next bar."""
    cfg = config or RsiEmaConfig()
    cfg.validate()
    bars = list(candles)
    if not bars:
        return []
    if any(not c.complete for c in bars):
        # Incomplete bars are ignored, not promoted to completed data.
        bars = [c for c in bars if c.complete]
    bars.sort(key=lambda c: c.open_time)
    for left, right in zip(bars, bars[1:]):
        if left.symbol != right.symbol or left.timeframe != right.timeframe:
            raise ValueError("Strategy input must contain one symbol and one timeframe")
        if right.open_time <= left.open_time:
            raise ValueError("Candles must have unique, ascending timestamps")
    closes = [c.close for c in bars]
    e_fast = ema(closes, cfg.ema_fast)
    e_mid = ema(closes, cfg.ema_mid)
    e_slow = ema(closes, cfg.ema_slow)
    rsi_values = rsi(closes, cfg.rsi_period)
    signals: list[StrategySignal] = []
    warmup = max(cfg.ema_slow, cfg.rsi_period + 1, cfg.volume_period + 1) + 1
    for i in range(warmup, len(bars)):
        ef, em, es = e_fast[i], e_mid[i], e_slow[i]
        rv = rsi_values[i]
        prior_rsi = rsi_values[i - 1]
        if None in (ef, em, es, rv, prior_rsi):
            continue
        # RSI > 70 is treated as momentum continuation, not an automatic reversal.
        if not (rv >= cfg.rsi_overbought and rv > prior_rsi):
            continue
        if not (ef > em > es and bars[i].close > ef):
            continue
        start = max(1, i - cfg.cross_lookback + 1)
        bullish_cross_seen = any(
            e_fast[j - 1] is not None and e_mid[j - 1] is not None
            and e_fast[j] is not None and e_mid[j] is not None
            and e_fast[j - 1] <= e_mid[j - 1] and e_fast[j] > e_mid[j]
            for j in range(start, i + 1)
        )
        if not bullish_cross_seen:
            continue
        # Breakout must use only the immediately previous completed candle.
        if bars[i].close <= bars[i - 1].high:
            continue
        volume_baseline = sum(c.volume for c in bars[i - cfg.volume_period:i]) / cfg.volume_period
        if volume_baseline <= 0 or bars[i].volume < volume_baseline * cfg.volume_multiplier:
            continue
        signals.append(StrategySignal(
            symbol=bars[i].symbol, timestamp=bars[i].close_time, candle_index=i,
            direction="LONG", reason=("RSI_OVERBOUGHT_RISING", "EMA21_CROSSED_ABOVE_EMA50",
            "EMA21_GT_EMA50_GT_EMA100", "CLOSE_ABOVE_EMAS", "PREVIOUS_HIGH_BREAKOUT", "RELATIVE_VOLUME_OK"),
            close=bars[i].close, rsi=float(rv), ema21=float(ef), ema50=float(em), ema100=float(es),
        ))
    return signals
