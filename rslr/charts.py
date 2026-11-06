from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Rectangle

from rslr.indicators import ema, rsi


def generate_chart(
    frame: pd.DataFrame,
    symbol: str,
    timeframe: str = "1H",
    output_dir: str = "charts",
    signal: dict | None = None,
) -> Path:
    if frame.empty:
        raise ValueError("No market candles are available for charting")
    data = frame.copy().sort_values("timestamp").tail(140).reset_index(drop=True)
    data["ema21"] = ema(data.close, 21)
    data["ema50"] = ema(data.close, 50)
    data["rsi14"] = rsi(data.close, 14)
    path_dir = Path(output_dir)
    path_dir.mkdir(parents=True, exist_ok=True)
    safe_symbol = symbol.replace("/", "_").replace(":", "_")
    path = path_dir / f"{safe_symbol}_{timeframe.lower()}.png"

    fig, (price_ax, rsi_ax, vol_ax) = plt.subplots(
        3, 1, figsize=(13, 9), sharex=True, gridspec_kw={"height_ratios": [5, 1.4, 1.3]}
    )
    width = 0.62
    for i, row in data.iterrows():
        up = row.close >= row.open
        lower = min(row.open, row.close)
        height = max(abs(row.close - row.open), row.close * 0.00002)
        price_ax.vlines(i, row.low, row.high, linewidth=0.8)
        price_ax.add_patch(Rectangle((i - width / 2, lower), width, height, fill=up, alpha=0.75))
    price_ax.plot(data.index, data.ema21, label="EMA 21", linewidth=1.1)
    price_ax.plot(data.index, data.ema50, label="EMA 50", linewidth=1.1)
    if signal and signal.get("reference_entry") is not None:
        price_ax.axhline(float(signal["reference_entry"]), linestyle="-.", linewidth=1.0, label="Signal entry")
        price_ax.axhline(float(signal["stop_loss"]), linestyle="--", linewidth=1.0, label="Stop loss")
        price_ax.axhline(float(signal["take_profit"]), linestyle="--", linewidth=1.0, label="Take profit")
        price_ax.scatter([len(data) - 1], [float(signal["reference_entry"])], marker="*", s=90, label="Entry marker")
    price_ax.set_title(f"{symbol} | {timeframe} | Latest completed candle: {data.timestamp.iloc[-1]}")
    price_ax.set_ylabel("Price")
    price_ax.legend(loc="upper left")
    price_ax.grid(alpha=0.2)

    rsi_ax.plot(data.index, data.rsi14, linewidth=1)
    rsi_ax.axhline(70, linestyle="--", linewidth=0.8)
    rsi_ax.axhline(30, linestyle="--", linewidth=0.8)
    rsi_ax.set_ylim(0, 100)
    rsi_ax.set_ylabel("RSI 14")
    rsi_ax.grid(alpha=0.2)

    vol_ax.bar(data.index, data.volume, width=0.7)
    vol_ax.set_ylabel("Volume")
    vol_ax.grid(alpha=0.2)
    ticks = list(range(0, len(data), max(1, len(data) // 8)))
    vol_ax.set_xticks(ticks)
    vol_ax.set_xticklabels([data.timestamp.iloc[i].strftime("%m-%d %H:%M") for i in ticks], rotation=20, ha="right")
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return path
