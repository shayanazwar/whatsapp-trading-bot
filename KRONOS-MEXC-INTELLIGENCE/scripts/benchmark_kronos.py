from __future__ import annotations
import argparse
import json
import resource
import time
from pathlib import Path
import pandas as pd
from app.forecasting.kronos import KronosForecastEngine
from app.market.mexc import MexcPublicClient

parser = argparse.ArgumentParser()
parser.add_argument("--symbol", default="BTC_USDT")
parser.add_argument("--samples", type=int, default=1000)
parser.add_argument("--horizon", type=int, default=24)
parser.add_argument("--csv", help="Optional local candle CSV; otherwise use MEXC public API")
args = parser.parse_args()
if args.csv:
    candles = pd.read_csv(args.csv)
else:
    with MexcPublicClient() as client:
        end = int(pd.Timestamp.now(tz="UTC").timestamp())
        candles = client.klines(args.symbol, "Min60", end - 512 * 3600, end)
start = time.perf_counter()
out = KronosForecastEngine().forecast(candles, samples=args.samples, horizon=args.horizon)
elapsed = time.perf_counter() - start
print(json.dumps({"symbol":args.symbol,"shape":list(out.paths.shape),"elapsed_seconds":elapsed,
                  "device":out.metadata["device"],"max_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  "summary":out.summary}, indent=2))
