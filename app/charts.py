from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import mkstemp
from typing import Any, Iterable

import matplotlib
matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import pandas as pd


class ChartRenderer:
    """Professional MEXC Futures chart renderer for WhatsApp."""

    def __init__(self, default_bars: int = 180) -> None:
        self.default_bars = max(50, min(int(default_bars), 500))

    async def render(
        self,
        ref: Any,
        timeframe: str,
        rows: Iterable,
        title: str | None = None,
    ) -> Path:
        return await asyncio.to_thread(
            self._render_sync,
            ref,
            timeframe,
            list(rows),
            title or f"{getattr(ref, 'exchange', 'MEXC').upper()} {getattr(ref, 'symbol', 'UNKNOWN')}",
        )

    @staticmethod
    def _normalize_row(row: Any) -> dict[str, Any] | None:
        try:
            if isinstance(row, dict):
                timestamp = (
                    row.get("time")
                    or row.get("timestamp")
                    or row.get("openTime")
                    or row.get("ts")
                )
                open_price = row.get("open")
                high_price = row.get("high")
                low_price = row.get("low")
                close_price = row.get("close")
                volume = row.get("volume") or row.get("vol") or 0
            else:
                if len(row) < 6:
                    return None
                timestamp, open_price, high_price, low_price, close_price, volume = row[:6]

            if timestamp is None:
                return None

            timestamp = int(float(timestamp))
            if timestamp < 10**12:
                timestamp *= 1000

            open_price = float(open_price)
            high_price = float(high_price)
            low_price = float(low_price)
            close_price = float(close_price)
            volume = float(volume or 0)

            if min(open_price, high_price, low_price, close_price) <= 0:
                return None
            if low_price > high_price:
                return None

            return {
                "Date": pd.to_datetime(timestamp, unit="ms", utc=True),
                "Open": open_price,
                "High": high_price,
                "Low": low_price,
                "Close": close_price,
                "Volume": max(volume, 0.0),
            }
        except (TypeError, ValueError, IndexError, KeyError):
            return None

    @staticmethod
    def _format_price(value: float) -> str:
        value = float(value)
        if value >= 1000:
            return f"{value:,.2f}"
        if value >= 100:
            return f"{value:,.3f}"
        if value >= 1:
            return f"{value:,.4f}"
        if value >= 0.1:
            return f"{value:.5f}"
        if value >= 0.01:
            return f"{value:.6f}"
        if value >= 0.001:
            return f"{value:.7f}"
        return f"{value:.8f}"

    @staticmethod
    def _format_volume(value: float) -> str:
        value = abs(float(value))
        if value >= 1_000_000_000:
            return f"{value / 1_000_000_000:.2f}B"
        if value >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        if value >= 1_000:
            return f"{value / 1_000:.2f}K"
        return f"{value:.0f}"

    def _render_sync(
        self,
        ref: Any,
        timeframe: str,
        rows: list,
        title: str,
    ) -> Path:
        if not rows:
            raise ValueError("No candle data returned.")

        data = []
        for row in rows:
            normalized = self._normalize_row(row)
            if normalized is not None:
                data.append(normalized)

        if not data:
            raise ValueError("No valid candle data returned.")

        frame = pd.DataFrame(data).set_index("Date").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]

        if len(frame) > self.default_bars:
            frame = frame.iloc[-self.default_bars:].copy()
        if len(frame) < 20:
            raise ValueError("Not enough candle data to render chart.")

        frame["EMA21"] = frame["Close"].ewm(span=21, adjust=False).mean()
        frame["EMA50"] = frame["Close"].ewm(span=50, adjust=False).mean()
        frame["EMA100"] = frame["Close"].ewm(span=100, adjust=False).mean()
        frame["EMA200"] = frame["Close"].ewm(span=200, adjust=False).mean()

        latest = frame.iloc[-1]
        previous_close = float(frame["Close"].iloc[-2])
        current_price = float(latest["Close"])
        change_pct = ((current_price - previous_close) / previous_close * 100) if previous_close else 0.0
        change_up = change_pct >= 0

        fd, path_str = mkstemp(prefix="chart_", suffix=".png")
        os.close(fd)
        output_path = Path(path_str)

        background = "#0b0f14"
        panel = "#0f141b"
        grid_color = "#26303a"
        text_color = "#d8dee7"
        muted_text = "#7f8a98"
        bullish = "#22c55e"
        bearish = "#ef4444"
        ema21_color = "#38bdf8"
        ema50_color = "#f59e0b"
        ema100_color = "#a78bfa"
        ema200_color = "#f472b6"
        current_color = bullish if change_up else bearish

        fig = plt.figure(figsize=(15.36, 8.64), dpi=150, facecolor=background)
        grid = fig.add_gridspec(2, 1, height_ratios=[4.8, 1.25], hspace=0.035)
        ax = fig.add_subplot(grid[0])
        vol_ax = fig.add_subplot(grid[1], sharex=ax)

        try:
            for axis in (ax, vol_ax):
                axis.set_facecolor(panel)
                axis.tick_params(colors=muted_text, labelsize=8, length=0)
                for spine in axis.spines.values():
                    spine.set_color(panel)

            ax.grid(True, axis="y", color=grid_color, alpha=0.45, linewidth=0.6)
            vol_ax.grid(True, axis="y", color=grid_color, alpha=0.35, linewidth=0.6)
            ax.set_axisbelow(True)
            vol_ax.set_axisbelow(True)

            dates = mdates.date2num(frame.index.to_pydatetime())
            if len(dates) > 1:
                step = float(pd.Series(dates).diff().median())
                candle_width = max(step * 0.68, 0.00001)
            else:
                candle_width = 0.02

            for x, (_, row) in zip(dates, frame.iterrows()):
                o = float(row["Open"])
                h = float(row["High"])
                l = float(row["Low"])
                c = float(row["Close"])
                up = c >= o
                candle_color = bullish if up else bearish

                ax.vlines(x, l, h, color=candle_color, linewidth=0.8, alpha=0.95)
                body_low = min(o, c)
                body_height = max(abs(c - o), max(abs(c) * 0.00001, 1e-12))
                rect = Rectangle(
                    (x - candle_width / 2, body_low),
                    candle_width,
                    body_height,
                    facecolor=candle_color,
                    edgecolor=candle_color,
                    linewidth=0.5,
                )
                ax.add_patch(rect)

                vol_ax.bar(
                    x,
                    float(row["Volume"]),
                    width=candle_width,
                    color=candle_color,
                    alpha=0.55,
                    align="center",
                )

            ax.plot(dates, frame["EMA21"], color=ema21_color, linewidth=1.25, label="EMA 21")
            ax.plot(dates, frame["EMA50"], color=ema50_color, linewidth=1.25, label="EMA 50")
            ax.plot(dates, frame["EMA100"], color=ema100_color, linewidth=1.15, label="EMA 100")
            ax.plot(dates, frame["EMA200"], color=ema200_color, linewidth=1.15, label="EMA 200")

            ax.axhline(current_price, color=current_color, linewidth=0.9, linestyle="--", alpha=0.8)
            ax.text(
                1.003,
                current_price,
                self._format_price(current_price),
                transform=ax.get_yaxis_transform(),
                va="center",
                ha="left",
                color=background,
                fontsize=8,
                fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.28", facecolor=current_color, edgecolor=current_color),
                clip_on=False,
            )

            ax.yaxis.tick_right()
            ax.yaxis.set_label_position("right")
            ax.tick_params(axis="x", labelbottom=False)
            vol_ax.yaxis.tick_right()

            locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
            formatter = mdates.ConciseDateFormatter(locator)
            vol_ax.xaxis.set_major_locator(locator)
            vol_ax.xaxis.set_major_formatter(formatter)
            vol_ax.tick_params(axis="x", colors=muted_text, labelsize=8)

            ax.legend(
                loc="upper left",
                frameon=False,
                fontsize=8,
                labelcolor=text_color,
                ncol=4,
            )

            exchange = getattr(ref, "exchange", "MEXC").upper()
            symbol = getattr(ref, "symbol", "UNKNOWN").upper()
            header = f"{symbol} â€¢ {timeframe.upper()}"
            subheader = f"{exchange} FUTURES"

            fig.text(0.055, 0.965, header, color=text_color, fontsize=16, fontweight="bold", va="top")
            fig.text(0.055, 0.935, subheader, color=muted_text, fontsize=8.5, va="top")

            ohlc = (
                f"O {self._format_price(float(latest['Open']))}   "
                f"H {self._format_price(float(latest['High']))}   "
                f"L {self._format_price(float(latest['Low']))}   "
                f"C {self._format_price(current_price)}   "
                f"{change_pct:+.2f}%"
            )
            fig.text(0.055, 0.905, ohlc, color=current_color, fontsize=9, fontweight="bold", va="top")

            fig.text(
                0.055,
                0.045,
                f"Volume {self._format_volume(float(latest['Volume']))}",
                color=muted_text,
                fontsize=8,
            )
            fig.text(
                0.945,
                0.045,
                "PAK TRADING ACADEMY â€¢ MEXC FUTURES",
                color=muted_text,
                fontsize=7.5,
                ha="right",
            )

            fig.subplots_adjust(left=0.055, right=0.925, top=0.86, bottom=0.10)
            fig.savefig(
                output_path,
                format="png",
                dpi=150,
                facecolor=background,
                bbox_inches="tight",
                pad_inches=0.12,
            )
            return output_path
        except Exception:
            try:
                output_path.unlink(missing_ok=True)
            finally:
                plt.close(fig)
            raise
        finally:
            if plt.fignum_exists(fig.number):
                plt.close(fig)
