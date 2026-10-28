"""Render higher-timeframe charts, isolated from strategy decisions."""
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


def _render_chart_for_timeframe(
    candles: Sequence[Candle], output_path: str | Path, *, timeframe: str, title_suffix: str = ""
) -> Path:
    tf = timeframe.upper()
    durations = {"1H": 3600, "4H": 4 * 3600, "12H": 12 * 3600, "1D": 24 * 3600, "5M": 300}
    if tf not in durations:
        raise ChartError("Unsupported chart timeframe")
    bars = sorted(candles, key=lambda c: c.open_time)
    if not bars:
        raise ChartError(f"No {tf} candles are available for charting")
    if any(c.timeframe != tf for c in bars):
        raise ChartError(f"Charting accepts only {tf} candles")
    if len(bars) < 200:
        raise ChartError(f"At least 200 {tf} candles are required for EMA200; received {len(bars)}")
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
    width_days = durations[tf] / 86400 * 0.72
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
    ax.set_title(f"{bars[-1].symbol} Futures — {tf} chart (visualization only) {title_suffix}".strip())
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


def render_timeframe_chart(
    candles: Sequence[Candle], output_path: str | Path, *, timeframe: str, title_suffix: str = ""
) -> Path:
    """Render 1H/4H/12H/1D charts; charts do not themselves create trading signals."""
    tf = timeframe.upper()
    if tf not in {"1H", "4H", "12H", "1D"}:
        raise ChartError("Strategy chart timeframe must be 1H, 4H, 12H or 1D")
    return _render_chart_for_timeframe(candles, output_path, timeframe=tf, title_suffix=title_suffix)


def render_5m_chart(candles: Sequence[Candle], output_path: str | Path, *, title_suffix: str = "") -> Path:
    """Legacy visualization-only renderer; the current command parser rejects 5M."""
    return _render_chart_for_timeframe(candles, output_path, timeframe="5M", title_suffix=title_suffix)
