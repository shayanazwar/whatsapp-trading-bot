# Original MEXC Futures strategy

The strategy is a transparent research hypothesis, not a claim of alpha.

- **1D regime:** EMA20/EMA50 relationship, close location and ATR context classify bullish, bearish, sideways or unknown.
- **12H direction:** same transparent trend classification, using 12H candles aggregated from three completed, contiguous UTC-aligned 4H candles.
- **4H setup:** in the direction agreed by 1D and 12H, detect trend pullback/value re-entry, liquidity sweep/reclaim, or breakout/retest. Swing structure and invalidation are retained in the decision output.
- **1H trigger:** require a completed 1H candle close beyond the prior candle extreme, directional candle body and a minimum relative-volume context.
- **Risk geometry:** structural stop from the 4H setup, TP1/TP2 based on risk multiples, rounded to the contract's supported tick/volume precision before evaluation.
- **Market context:** spread, order-book availability, funding and contract metadata are gates or context fields; missing data is represented as missing, never fabricated.

A final LONG/SHORT research candidate requires both 1D and 12H direction agreement and a valid 4H setup plus 1H trigger. Otherwise the engine returns WAIT, NO_TRADE or INSUFFICIENT_DATA with explicit reasons. The score is a transparent heuristic, not a fitted probability.

The first research defaults are a 1.5 minimum net RR and a 0.5% paper-account risk budget. These are configurable assumptions, not advice or proof of profitability.
