from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import mkstemp
from typing import Iterable

import matplotlib
matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import pandas as pd

from .market import MarketRef


class ChartRenderer:
    def __init__(self, default_bars: int = 180) -> None:
        self.default_bars = max(50, min(default_bars, 500))

    async def render(self, ref: MarketRef, timeframe: str, rows: Iterable, title: str | None = None) -> Path:
        return await asyncio.to_thread(
            self._render_sync, ref, timeframe, list(rows), title or f"{ref.exchange.upper()} {ref.symbol}"
        )

    def _render_sync(self, ref: MarketRef, timeframe: str, rows: list, title: str) -> Path:
        if not rows:
            raise ValueError("No candle data returned.")

        data = []
        for row in rows:
            data.append({
                "Date": pd.to_datetime(int(row[0]), unit="ms", utc=True),
                "Open": float(row[1]),
                "High": float(row[2]),
                "Low": float(row[3]),
                "Close": float(row[4]),
                "Volume": float(row[5]),
            })
        frame = pd.DataFrame(data).set_index("Date")
        frame = frame[~frame.index.duplicated(keep="last")]
        frame["EMA21"] = frame["Close"].ewm(span=21, adjust=False).mean()
        frame["EMA50"] = frame["Close"].ewm(span=50, adjust=False).mean()

        fd, path_str = mkstemp(prefix="chart_", suffix=".png")
        os.close(fd)
        Path(path_str).unlink(missing_ok=True)

        fig = plt.figure(figsize=(12.8, 7.2), dpi=150)
        grid = fig.add_gridspec(4, 1, height_ratios=[3.2, 1, 0.05, 0.05], hspace=0.02)
        ax = fig.add_subplot(grid[0])
        vol_ax = fig.add_subplot(grid[1], sharex=ax)

        x = mdates.date2num(frame.index.to_pydatetime())
        if len(x) > 1:
            step = pd.Series(frame.index).diff().dropna().median().total_seconds() / 86400.0
            width = max(step * 0.62, 0.0002)
        else:
            width = 0.02

        for xi, (_, row) in zip(x, frame.iterrows()):
            up = row["Close"] >= row["Open"]
            body_low = min(row["Open"], row["Close"])
            body_height = abs(row["Close"] - row["Open"]) or max(abs(row["Close"]) * 0.000001, 1e-12)
            ax.vlines(xi, row["Low"], row["High"], linewidth=0.8)
            rect = Rectangle((xi - width / 2, body_low), width, body_height, linewidth=0.7, fill=True, alpha=0.85)
            rect.set_facecolor("#24a148" if up else "#da1e28")
            rect.set_edgecolor("#24a148" if up else "#da1e28")
            ax.add_patch(rect)

        ax.plot(frame.index, frame["EMA21"], linewidth=1.0, label="EMA 21")
        ax.plot(frame.index, frame["EMA50"], linewidth=1.0, label="EMA 50")
        ax.set_title(f"{title} • {timeframe.upper()}")
        ax.set_ylabel("Price")
        ax.grid(True, alpha=0.16)
        ax.legend(loc="upper left", frameon=False)

        colors = ["#24a148" if c >= o else "#da1e28" for o, c in zip(frame["Open"], frame["Close"])]
        vol_ax.bar(frame.index, frame["Volume"], width=width, alpha=0.65, color=colors, align="center")
        vol_ax.set_ylabel("Volume")
        vol_ax.grid(True, alpha=0.12)
        plt.setp(ax.get_xticklabels(), visible=False)
        vol_ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=frame.index.tz))
        fig.text(0.99, 0.01, "Market data: Binance public spot API", ha="right", va="bottom", fontsize=7, alpha=0.6)
        fig.savefig(path_str, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return Path(path_str)
