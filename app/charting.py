"""MEXC 5M charting only. This module is not imported by the strategy engine."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from app.market.candles import Candle
from app.market.indicators import ema, confirmed_pivots


class ChartError(RuntimeError):
    pass


def render_5m_chart(candles: Sequence[Candle], output_path: str | Path, *, title_suffix: str = "") -> Path:
    """Render 5-minute candles and display-only EMA 21/50/100/200 plus pivot S/R.

    The function only accepts 5M candles and produces an image. It does not call or mutate
    strategy modules and its results must never be forwarded into a strategy decision.
    """
    bars = sorted(candles, key=lambda c: c.open_time)
    if not bars:
        raise ChartError("No 5M candles are available for charting")
    if any(c.timeframe != "5M" for c in bars):
        raise ChartError("Charting accepts only 5M candles")
    if len(bars) < 200:
        raise ChartError(f"At least 200 5M candles are required for EMA200; received {len(bars)}")
    closes = [c.close for c in bars]
    ema_series = {period: ema(closes, period) for period in (21, 50, 100, 200)}
    highs = [c.high for c in bars]
    lows = [c.low for c in bars]
    ph, pl = confirmed_pivots(highs, lows, 3)
    # Keep recent distinct pivot prices; chart levels are visualization annotations, not signals.
    supports: list[float] = []
    resistances: list[float] = []
    for idx in pl[-30:]:
        price = lows[idx]
        if all(abs(price - old) / price > 0.002 for old in supports):
            supports.append(price)
    for idx in ph[-30:]:
        price = highs[idx]
        if all(abs(price - old) / price > 0.002 for old in resistances):
            resistances.append(price)
    dates = [datetime.fromtimestamp(c.open_time, tz=timezone.utc) for c in bars]
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(14, 8), dpi=140)
    width_days = (5 * 60) / 86400 * 0.72
    for date, candle in zip(dates, bars):
        color = "#1b9e77" if candle.close >= candle.open else "#d95f02"
        x = mdates.date2num(date)
        ax.vlines(x, candle.low, candle.high, color=color, linewidth=0.7, alpha=0.95)
        body_low = min(candle.open, candle.close)
        body_height = max(abs(candle.close - candle.open), max(candle.close, candle.open) * 0.00001)
        rect = plt.Rectangle((x - width_days / 2, body_low), width_days, body_height, facecolor=color, edgecolor=color, linewidth=0.5)
        ax.add_patch(rect)
    line_colors = {21: "#377eb8", 50: "#984ea3", 100: "#e6ab02", 200: "#4d4d4d"}
    for period, values in ema_series.items():
        valid = [(dates[i], val) for i, val in enumerate(values) if val is not None]
        if valid:
            ax.plot([x[0] for x in valid], [x[1] for x in valid], label=f"EMA {period}", color=line_colors[period], linewidth=1.15)
    for price in supports[-5:]:
        ax.axhline(price, color="#1b9e77", linestyle="--", linewidth=0.75, alpha=0.65)
    for price in resistances[-5:]:
        ax.axhline(price, color="#d95f02", linestyle="--", linewidth=0.75, alpha=0.65)
    ax.set_title(f"{bars[-1].symbol} Futures — 5M chart (visualization only) {title_suffix}".strip())
    ax.set_ylabel("Price")
    ax.set_xlabel("Time (UTC)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=timezone.utc))
    ax.grid(True, alpha=0.2)
    ax.legend(loc="upper left", ncol=4)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out, format="png")
    plt.close(fig)
    return out
