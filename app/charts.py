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
    """
    Professional MEXC Futures chart renderer.

    Design:
    - Dark trading-terminal style
    - Price axis on RIGHT
    - Candles occupy the main visual area
    - EMA 21 / 50 / 100 / 200
    - Current price line + label
    - Professional OHLC header
    - Volume panel
    - Clean date axis
    - WhatsApp-friendly 16:9 output
    - Supports both MEXC list candles and normalized dict candles
    """

    def __init__(self, default_bars: int = 180) -> None:
        self.default_bars = max(
            50,
            min(int(default_bars), 500),
        )

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
            title
            or f"{getattr(ref, 'exchange', 'MEXC').upper()} "
               f"{getattr(ref, 'symbol', 'UNKNOWN')}",
        )

    # ============================================================
    # NORMALIZE CANDLES
    # ============================================================

    @staticmethod
    def _normalize_row(row: Any) -> dict[str, Any] | None:
        """
        Accept:

        MEXC list format:
        [timestamp, open, high, low, close, volume, ...]

        Or dictionary format:
        {
            "time": ...,
            "open": ...,
            "high": ...,
            "low": ...,
            "close": ...,
            "volume": ...
        }
        """

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
                volume = (
                    row.get("volume")
                    or row.get("vol")
                    or 0
                )

            else:
                if len(row) < 6:
                    return None

                timestamp = row[0]
                open_price = row[1]
                high_price = row[2]
                low_price = row[3]
                close_price = row[4]
                volume = row[5]

            timestamp = int(float(timestamp))

            # Accept seconds or milliseconds.
            if timestamp < 10**12:
                timestamp *= 1000

            open_price = float(open_price)
            high_price = float(high_price)
            low_price = float(low_price)
            close_price = float(close_price)
            volume = float(volume or 0)

            if (
                open_price <= 0
                or high_price <= 0
                or low_price <= 0
                or close_price <= 0
            ):
                return None

            if low_price > high_price:
                return None

            return {
                "Date": pd.to_datetime(
                    timestamp,
                    unit="ms",
                    utc=True,
                ),
                "Open": open_price,
                "High": high_price,
                "Low": low_price,
                "Close": close_price,
                "Volume": max(volume, 0.0),
            }

        except (
            TypeError,
            ValueError,
            IndexError,
            KeyError,
        ):
            return None

    # ============================================================
    # FORMAT PRICE
    # ============================================================

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

    # ============================================================
    # FORMAT VOLUME
    # ============================================================

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

    # ============================================================
    # MAIN RENDERER
    # ============================================================

    def _render_sync(
        self,
        ref: Any,
        timeframe: str,
        rows: list,
        title: str,
    ) -> Path:

        if not rows:
            raise ValueError(
                "No candle data returned."
            )

        # --------------------------------------------------------
        # Normalize
        # --------------------------------------------------------

        data = []

        for row in rows:
            normalized = self._normalize_row(row)

            if normalized is not None:
                data.append(normalized)

        if not data:
            raise ValueError(
                "No valid candle data returned."
            )

        frame = (
            pd.DataFrame(data)
            .set_index("Date")
            .sort_index()
        )

        frame = frame[
            ~frame.index.duplicated(
                keep="last"
            )
        ]

        # --------------------------------------------------------
        # Limit visible candles
        # --------------------------------------------------------

        if len(frame) > self.default_bars:
            frame = frame.iloc[
                -self.default_bars:
            :].copy()

        if len(frame) < 20:
            raise ValueError(
                "Not enough candle data to render chart."
            )

        # --------------------------------------------------------
        # Indicators
        # --------------------------------------------------------

        frame["EMA21"] = (
            frame["Close"]
            .ewm(
                span=21,
                adjust=False,
            )
            .mean()
        )

        frame["EMA50"] = (
            frame["Close"]
            .ewm(
                span=50,
                adjust=False,
            )
            .mean()
        )

        frame["EMA100"] = (
            frame["Close"]
            .ewm(
                span=100,
                adjust=False,
            )
            .mean()
        )

        frame["EMA200"] = (
            frame["Close"]
            .ewm(
                span=200,
                adjust=False,
            )
            .mean()
        )

        # --------------------------------------------------------
        # Current candle information
        # --------------------------------------------------------

        latest = frame.iloc[-1]

        current_price = float(
            latest["Close"]
        )

        previous_close = float(
            frame["Close"].iloc[-2]
        )

        change_pct = (
            (
                current_price
                - previous_close
            )
            / previous_close
            * 100
            if previous_close
            else 0.0
        )

        change_up = change_pct >= 0

        # --------------------------------------------------------
        # Temporary output file
        # --------------------------------------------------------

        fd, path_str = mkstemp(
            prefix="chart_",
            suffix=".png",
        )

        os.close(fd)

        Path(path_str).unlink(
            missing_ok=True
        )

        # ========================================================
        # FIGURE
        # ========================================================

        fig = plt.figure(
            figsize=(15.36, 8.64),
            dpi=150,
            facecolor="#0b0f14",
        )

        grid = fig.add_gridspec(
            2,
            1,
            height_ratios=[
                4.8,
                1.25,
            ],
            hspace=0.035,
        )

        ax = fig.add_subplot(
            grid[0]
        )

        vol_ax = fig.add_subplot(
            grid[1],
            sharex=ax,
        )

        # ========================================================
        # DARK TERMINAL COLORS
        # ========================================================

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

        current_color = (
            bullish
            if change_up
            else bearish
        )

        # ========================================================
        # AXIS STYLING
        # ========================================================

        for axis in (
            ax,
            vol_ax,
        ):
            axis.set_facecolor(panel)

            axis.tick_params(
                colors=muted_text,
                labelsize=8,
                length=0,
            )

            axis
