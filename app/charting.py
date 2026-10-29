"""Render charts on the strategy-approved timeframes only.

Charting is a presentation layer: it does not make or mutate strategy decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from app.market.candles import INTERVAL_SECONDS, Candle
from app.market.indicators import ema, rsi, confirmed_pivots


CHART_TIMEFRAMES = frozenset({"5M", "15M", "1H", "4H", "8H", "12H", "1D", "3D", "1W", "1MO"})


class ChartError(RuntimeError):
    """Raised when a chart cannot be rendered honestly from the supplied candles."""


def render_candle_chart(
    candles: Sequence[Candle],
    output_path: str | Path,
    *,
    timeframe: str | None = None,
    title_suffix: str = "",
) -> Path:
    """Render completed MEXC Futures candles with EMA 21/50/100/200 and RSI divergence.

    Derived 12H/3D bars must be made only from complete, UTC-aligned source candles.
    The function never requests market data and never invokes the strategy engine.
    """
    bars = sorted((c for c in candles if c.complete), key=lambda c: c.open_time)
    if not bars:
        raise ChartError("No completed candles are available for charting")
    actual_tf = (timeframe or bars[-1].timeframe).upper()
    if actual_tf not in CHART_TIMEFRAMES:
        raise ChartError("Charts support 5M, 15M, 1H, 4H, 8H, 12H, 1D, 3D, 1W and 1MO")
    if any(c.timeframe != actual_tf for c in bars):
        raise ChartError(f"Every chart candle must use timeframe {actual_tf}")
    opens = [c.open_time for c in bars]
    if len(opens) != len(set(opens)):
        raise ChartError("Duplicate candle timestamps prevent a reliable chart")
    if len(bars) < 30:
        raise ChartError(f"At least 30 completed {actual_tf} candles are required; received {len(bars)}")
    if any(not all(v > 0 for v in (c.open, c.high, c.low, c.close)) for c in bars):
        raise ChartError("Candle prices must be positive")

    dates = [datetime.fromtimestamp(c.open_time, tz=timezone.utc) for c in bars]
    x_values = [mdates.date2num(d) for d in dates]
    width_days = INTERVAL_SECONDS[actual_tf] / 86400 * 0.72
    closes = [c.close for c in bars]
    ema_series = {period: ema(closes, period) for period in (21, 50, 100, 200)}
    rsi_series = rsi(closes, 14)
    highs = [c.high for c in bars]
    lows = [c.low for c in bars]
    ph, pl = confirmed_pivots(highs, lows, 3)

    supports: list[float] = []
    resistances: list[float] = []
    for idx in pl[-30:]:
        price = lows[idx]
        if price > 0 and all(abs(price - old) / price > 0.002 for old in supports):
            supports.append(price)
    for idx in ph[-30:]:
        price = highs[idx]
        if price > 0 and all(abs(price - old) / price > 0.002 for old in resistances):
            resistances.append(price)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, (ax, volume_ax, rsi_ax) = plt.subplots(
        3, 1, figsize=(14, 10), dpi=140, sharex=True,
        gridspec_kw={"height_ratios": [4, 1, 1.35]},
    )
    for x, candle in zip(x_values, bars):
        color = "#16a085" if candle.close >= candle.open else "#e05d44"
        ax.vlines(x, candle.low, candle.high, color=color, linewidth=0.8, alpha=0.95)
        body_low = min(candle.open, candle.close)
        body_height = max(abs(candle.close - candle.open), max(candle.close, candle.open) * 1e-6)
        ax.add_patch(Rectangle(
            (x - width_days / 2, body_low), width_days, body_height,
            facecolor=color, edgecolor=color, linewidth=0.5,
        ))
    line_colors = {21: "#377eb8", 50: "#984ea3", 100: "#e6ab02", 200: "#4d4d4d"}
    for period, values in ema_series.items():
        valid = [(dates[i], val) for i, val in enumerate(values) if val is not None]
        if valid:
            ax.plot([x[0] for x in valid], [x[1] for x in valid],
                    label=f"EMA {period}", color=line_colors[period], linewidth=1.15)
    for price in supports[-5:]:
        ax.axhline(price, color="#16a085", linestyle="--", linewidth=0.75, alpha=0.6)
    for price in resistances[-5:]:
        ax.axhline(price, color="#e05d44", linestyle="--", linewidth=0.75, alpha=0.6)
    volumes = [max(float(c.volume), 0.0) for c in bars]
    volume_colors = ["#16a085" if c.close >= c.open else "#e05d44" for c in bars]
    volume_ax.bar(x_values, volumes, width=width_days, color=volume_colors, alpha=0.8)
    suffix = f" {title_suffix}" if title_suffix else ""
    ax.set_title(f"MEXC Futures | {bars[-1].symbol} | {actual_tf} | completed candles only{suffix}")
    ax.set_ylabel("Price")
    volume_ax.set_ylabel("Volume")
    valid_rsi = [(i, float(value)) for i, value in enumerate(rsi_series) if value is not None]
    if valid_rsi:
        rsi_ax.plot([dates[i] for i, _ in valid_rsi], [v for _, v in valid_rsi],
                    color="#6a3d9a", linewidth=1.2, label="RSI 14")
    rsi_ax.axhline(70, color="#e05d44", linestyle="--", linewidth=0.8)
    rsi_ax.axhline(30, color="#16a085", linestyle="--", linewidth=0.8)
    rsi_ax.set_ylim(0, 100)
    rsi_ax.set_ylabel("RSI 14")
    rsi_ax.set_xlabel("Time (UTC)")
    # Mark confirmed divergence between the last two confirmed price pivots.
    high_pivots, low_pivots = confirmed_pivots(highs, lows, 2)
    rsi_points = {i: value for i, value in valid_rsi}
    if len(low_pivots) >= 2:
        p1, p2 = low_pivots[-2], low_pivots[-1]
        if p1 in rsi_points and p2 in rsi_points and lows[p2] < lows[p1] and rsi_points[p2] > rsi_points[p1]:
            rsi_ax.annotate("Bullish divergence", (dates[p2], rsi_points[p2]),
                            xytext=(8, 12), textcoords="offset points", fontsize=8,
                            arrowprops={"arrowstyle": "->", "linewidth": 0.8})
            ax.annotate("Bull div", (dates[p2], lows[p2]), xytext=(4, -18),
                        textcoords="offset points", fontsize=7, arrowprops={"arrowstyle": "->", "linewidth": 0.7})
    if len(high_pivots) >= 2:
        p1, p2 = high_pivots[-2], high_pivots[-1]
        if p1 in rsi_points and p2 in rsi_points and highs[p2] > highs[p1] and rsi_points[p2] < rsi_points[p1]:
            rsi_ax.annotate("Bearish divergence", (dates[p2], rsi_points[p2]),
                            xytext=(8, -16), textcoords="offset points", fontsize=8,
                            arrowprops={"arrowstyle": "->", "linewidth": 0.8})
            ax.annotate("Bear div", (dates[p2], highs[p2]), xytext=(4, 12),
                        textcoords="offset points", fontsize=7, arrowprops={"arrowstyle": "->", "linewidth": 0.7})
    volume_ax.set_xlabel("")
    rsi_ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d %H:%M", tz=timezone.utc))
    ax.grid(True, alpha=0.2)
    volume_ax.grid(True, axis="y", alpha=0.2)
    rsi_ax.grid(True, alpha=0.2)
    if valid_rsi:
        rsi_ax.legend(loc="upper left")
    if any(any(v is not None for v in series) for series in ema_series.values()):
        ax.legend(loc="upper left", ncol=4)
    fig.autofmt_xdate()
    fig.tight_layout()
    try:
        fig.savefig(out, format="png")
    finally:
        plt.close(fig)
    return out
