"""Render high-resolution MEXC Futures charts on the approved signal timeframes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.patches import Rectangle

from app.market.candles import INTERVAL_SECONDS, Candle
from app.market.indicators import ema, rsi, confirmed_pivots

CHART_TIMEFRAMES = frozenset({"1H", "4H", "12H", "1D"})

# Palette intentionally follows the previous dark MEXC chart style.
BACKGROUND = "#0b0f14"
PANEL = "#10161f"
GRID = "#283241"
TEXT = "#d7dee8"
MUTED = "#8b98aa"
UP = "#22c55e"
DOWN = "#ef4444"
EMA_COLORS = {21: "#38bdf8", 50: "#f59e0b", 100: "#a78bfa", 200: "#f472b6"}


class ChartError(RuntimeError):
    """Raised when a chart cannot be rendered honestly from the supplied candles."""


def _latest_divergence_pair(
    highs: Sequence[float],
    lows: Sequence[float],
    rsi_values: Sequence[float | None],
    *,
    pivot_width: int = 5,
    minimum_separation: int = 5,
    maximum_separation: int = 60,
) -> list[dict[str, object]]:
    """Return the most recent confirmed regular bullish/bearish RSI divergence.

    Price pivots are confirmed only after `pivot_width` bars to the right have
    closed. RSI is sampled at those same pivot bars to avoid look-ahead and
    mismatched pivot dates. The separation bounds follow the supplied
    divergence-indicator reference (5..60 bars).
    """
    high_pivots, low_pivots = confirmed_pivots(highs, lows, pivot_width)
    found: list[dict[str, object]] = []
    for kind, pivots, prices in (("Bear", high_pivots, highs), ("Bull", low_pivots, lows)):
        for previous, current in zip(pivots, pivots[1:]):
            distance = current - previous
            if not minimum_separation <= distance <= maximum_separation:
                continue
            rsi_previous = rsi_values[previous] if previous < len(rsi_values) else None
            rsi_current = rsi_values[current] if current < len(rsi_values) else None
            if rsi_previous is None or rsi_current is None:
                continue
            if kind == "Bear":
                is_divergence = prices[current] > prices[previous] and float(rsi_current) < float(rsi_previous)
            else:
                is_divergence = prices[current] < prices[previous] and float(rsi_current) > float(rsi_previous)
            if is_divergence and current >= len(highs) - maximum_separation - pivot_width:
                found.append({
                    "kind": kind, "first": previous, "second": current,
                    "first_rsi": float(rsi_previous), "second_rsi": float(rsi_current),
                })
    # At most one of each type is drawn; each is the newest valid confirmed pair.
    latest: dict[str, dict[str, object]] = {}
    for item in found:
        kind = str(item["kind"])
        if kind not in latest or int(item["second"]) > int(latest[kind]["second"]):
            latest[kind] = item
    return sorted(latest.values(), key=lambda item: int(item["second"]))


def render_candle_chart(
    candles: Sequence[Candle],
    output_path: str | Path,
    *,
    timeframe: str | None = None,
    title_suffix: str = "",
) -> Path:
    """Render OHLC candles, EMA 21/50/100/200, volume, RSI(14), and swing levels.

    All plotted bars must be completed and use one approved timeframe. The function
    does not request data or make strategy decisions.
    """
    bars = sorted((c for c in candles if c.complete), key=lambda c: c.open_time)
    if not bars:
        raise ChartError("No completed candles are available for charting")
    actual_tf = (timeframe or bars[-1].timeframe).upper()
    if actual_tf not in CHART_TIMEFRAMES:
        raise ChartError("Charts support 1H, 4H, 12H and 1D only")
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
    width_days = INTERVAL_SECONDS[actual_tf] / 86400 * 0.68
    opens_px = [c.open for c in bars]
    highs = [c.high for c in bars]
    lows = [c.low for c in bars]
    closes = [c.close for c in bars]
    volumes = [max(float(c.volume), 0.0) for c in bars]
    ema_series = {period: ema(closes, period) for period in (21, 50, 100, 200)}
    rsi_series = rsi(closes, 14)
    last = bars[-1]
    change_pct = (last.close / bars[-2].close - 1.0) * 100.0 if len(bars) > 1 and bars[-2].close > 0 else 0.0

    pivot_highs, pivot_lows = confirmed_pivots(highs, lows, 3)
    supports: list[float] = []
    resistances: list[float] = []
    for idx in pivot_lows[-30:]:
        level = lows[idx]
        if all(abs(level - old) / max(level, 1e-12) > 0.002 for old in supports):
            supports.append(level)
    for idx in pivot_highs[-30:]:
        level = highs[idx]
        if all(abs(level - old) / max(level, 1e-12) > 0.002 for old in resistances):
            resistances.append(level)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(16, 9.5), dpi=150, facecolor=BACKGROUND)
    grid = fig.add_gridspec(3, 1, height_ratios=[4.7, 1.05, 1.35], hspace=0.04)
    ax = fig.add_subplot(grid[0, 0])
    volume_ax = fig.add_subplot(grid[1, 0], sharex=ax)
    rsi_ax = fig.add_subplot(grid[2, 0], sharex=ax)
    for axis in (ax, volume_ax, rsi_ax):
        axis.set_facecolor(PANEL)
        axis.tick_params(colors=MUTED, labelsize=8)
        for spine in axis.spines.values():
            spine.set_color(GRID)

    for x, candle in zip(x_values, bars):
        color = UP if candle.close >= candle.open else DOWN
        ax.vlines(x, candle.low, candle.high, color=color, linewidth=0.85, alpha=0.98, zorder=2)
        body_low = min(candle.open, candle.close)
        body_height = max(abs(candle.close - candle.open), max(candle.close, candle.open) * 1e-6)
        ax.add_patch(Rectangle(
            (x - width_days / 2, body_low), width_days, body_height,
            facecolor=color, edgecolor=color, linewidth=0.45, zorder=3,
        ))

    for period, values in ema_series.items():
        valid = [(i, float(value)) for i, value in enumerate(values) if value is not None]
        if valid:
            ax.plot([x_values[i] for i, _ in valid], [value for _, value in valid],
                    label=f"EMA {period}", color=EMA_COLORS[period], linewidth=1.35, zorder=4)

    # Keep only a few relevant, distinct swing levels to avoid overloading the chart.
    for level in supports[-3:]:
        ax.axhline(level, color=UP, linestyle=(0, (4, 3)), linewidth=0.8, alpha=0.45, zorder=1)
    for level in resistances[-3:]:
        ax.axhline(level, color=DOWN, linestyle=(0, (4, 3)), linewidth=0.8, alpha=0.45, zorder=1)

    move_color = UP if change_pct >= 0 else DOWN
    ax.axhline(last.close, color=move_color, linestyle=(0, (4, 3)), linewidth=0.9, alpha=0.9, zorder=1)
    ax.text(0.99, 0.035, f"LAST  {last.close:,.8g}", transform=ax.transAxes,
            ha="right", va="bottom", color=move_color, fontsize=9, fontweight="bold",
            bbox={"facecolor": BACKGROUND, "edgecolor": move_color, "boxstyle": "round,pad=0.3", "alpha": 0.95})

    candle_colors = [UP if c.close >= c.open else DOWN for c in bars]
    volume_ax.bar(x_values, volumes, width=width_days, color=candle_colors, alpha=0.78, linewidth=0)
    volume_ax.set_ylabel("Volume", color=MUTED, fontsize=8, labelpad=8)

    valid_rsi = [(i, float(value)) for i, value in enumerate(rsi_series) if value is not None]
    if valid_rsi:
        rsi_ax.plot([x_values[i] for i, _ in valid_rsi], [value for _, value in valid_rsi],
                    color="#c4b5fd", linewidth=1.3, label="RSI 14")
    rsi_ax.axhline(70, color=DOWN, linestyle=(0, (4, 3)), linewidth=0.9, alpha=0.8)
    rsi_ax.axhline(50, color=MUTED, linestyle=":", linewidth=0.65, alpha=0.55)
    rsi_ax.axhline(30, color=UP, linestyle=(0, (4, 3)), linewidth=0.9, alpha=0.8)
    rsi_ax.fill_between(x_values, 30, 70, color="#64748b", alpha=0.06)
    rsi_ax.set_ylim(0, 100)
    rsi_ax.set_ylabel("RSI 14", color=MUTED, fontsize=8, labelpad=8)
    rsi_ax.set_xlabel("Time (UTC)", color=MUTED, fontsize=8)

    # Confirmed RSI divergences use 5-left/5-right price pivots and a 5..60-bar
    # separation. Trend lines are drawn on both price and RSI panels, with a
    # visible Bull/Bear Divergence badge.
    divergence_pairs = _latest_divergence_pair(highs, lows, rsi_series)
    divergence_colors = {"Bull": UP, "Bear": DOWN}
    for divergence in divergence_pairs:
        kind = str(divergence["kind"])
        first, second = int(divergence["first"]), int(divergence["second"])
        first_rsi = float(divergence["first_rsi"])
        second_rsi = float(divergence["second_rsi"])
        color = divergence_colors[kind]
        price_points = lows if kind == "Bull" else highs
        ax.plot([x_values[first], x_values[second]], [price_points[first], price_points[second]],
                color=color, linewidth=1.8, alpha=0.9, zorder=5)
        ax.scatter([x_values[first], x_values[second]], [price_points[first], price_points[second]],
                   color=color, s=18, zorder=6, edgecolors=BACKGROUND, linewidths=0.4)
        rsi_ax.plot([x_values[first], x_values[second]], [first_rsi, second_rsi],
                    color=color, linewidth=2.4, zorder=5)
        rsi_ax.scatter([x_values[first], x_values[second]], [first_rsi, second_rsi],
                       color=color, s=22, zorder=6, edgecolors=BACKGROUND, linewidths=0.5)
        label_offset = (10, 12) if kind == "Bull" else (10, -23)
        rsi_ax.annotate(
            f"{kind} Divergence", (x_values[second], second_rsi),
            xytext=label_offset, textcoords="offset points", fontsize=8,
            color="#ffffff", fontweight="bold", clip_on=False,
            bbox={"boxstyle": "round,pad=0.30", "facecolor": color,
                  "edgecolor": color, "alpha": 0.96},
            arrowprops={"arrowstyle": "-", "linewidth": 1.0, "color": color},
        )

    suffix = f" | {title_suffix}" if title_suffix else ""
    fig.suptitle(f"{last.symbol}  ·  {actual_tf}  |  MEXC FUTURES{suffix}",
                 x=0.065, y=0.985, ha="left", color=TEXT, fontsize=16, fontweight="bold")
    candle_label_color = UP if change_pct >= 0 else DOWN
    fig.text(0.066, 0.948,
             f"O  {last.open:,.8g}    H  {last.high:,.8g}    L  {last.low:,.8g}    C  {last.close:,.8g}    {change_pct:+.2f}%",
             ha="left", va="center", color=candle_label_color, fontsize=9, fontweight="medium")
    ax.set_ylabel("Price", color=MUTED, fontsize=8, labelpad=8)
    # Put the price scale on the right side, matching a conventional trading chart.
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.tick_params(axis="y", labelleft=False, labelright=True)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda value, _pos: f"{value:,.8g}"))
    ax.grid(True, color=GRID, linewidth=0.65, alpha=0.72)
    volume_ax.grid(True, axis="y", color=GRID, linewidth=0.55, alpha=0.7)
    rsi_ax.grid(True, color=GRID, linewidth=0.55, alpha=0.7)
    ax.set_axisbelow(True)
    volume_ax.set_axisbelow(True)
    rsi_ax.set_axisbelow(True)
    ax.tick_params(labelbottom=False)
    volume_ax.tick_params(labelbottom=False)
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8, tz=timezone.utc)
    rsi_ax.xaxis.set_major_locator(locator)
    rsi_ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M" if actual_tf in {"1H", "4H"} else "%b %d", tz=timezone.utc))
    ax.legend(loc="upper left", ncol=4, frameon=True, facecolor=BACKGROUND, edgecolor=GRID,
              labelcolor=TEXT, fontsize=8)
    if valid_rsi:
        rsi_ax.legend(loc="upper left", frameon=False, labelcolor=TEXT, fontsize=8)
    ax.margins(x=0.012)
    fig.subplots_adjust(left=0.065, right=0.97, top=0.91, bottom=0.075)
    try:
        fig.savefig(out, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    finally:
        plt.close(fig)
    return out
