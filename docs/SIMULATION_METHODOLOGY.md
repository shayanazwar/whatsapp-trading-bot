# Historical scenario simulation

The Time Machine uses a moving-block bootstrap over normalized OHLC bar ratios. Each path concatenates contiguous historical blocks, preserving local sequence and some short-range serial dependence. The current-volatility matching logic selects blocks from a subset of similar rolling-volatility conditions. It is a scenario generator, not a causal market model and not a prediction engine.

The simulator records its random seed, block length, sample count, horizon and source-data window. TP/SL barrier races are computed separately for Kronos model paths and historical bootstrap paths. If one candle touches stop and target, the outcome is marked ambiguous and conservatively treated as stop-first in the net-R summary. Missing intrabar sequencing cannot be inferred from OHLC candles.
