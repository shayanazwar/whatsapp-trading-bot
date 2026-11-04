# Original MEXC Futures strategy

The strategy is a transparent research hypothesis, not a claim of alpha.

- **1D regime:** EMA20/EMA50 relationship, close location and ATR context classify bullish, bearish, sideways or unknown.
- **12H direction:** same transparent trend classification, using 12H candles aggregated from three completed, contiguous UTC-aligned 4H candles.
- **4H setup:** in the direction agreed by 1D and 12H, detect trend pullback/value re-entry, liquidity sweep/reclaim, or breakout/retest. Swing structure and invalidation are retained in the decision output.
- **1H trigger:** require a completed 1H candle close beyond the prior candle extreme, directional candle body and a minimum relative-volume context.
- **Risk geometry:** structural stop from the 4H setup, TP1/TP2 based on risk multiples, rounded to the contract's supported tick/volume precision before evaluation.
- **Market context:** spread, fresh side-specific order-book depth, current funding rate and verified contract metadata are gates or context fields. Unknown values remain unknown. A candidate is not approved using an assumed zero funding rate.
- **Visible-book gate:** immediately before paper approval, the system estimates quote-side notional depth from MEXC depth levels (`[price, order_count, contract_quantity]`) inside 25 bps of mid. LONG candidates consume asks; SHORT candidates consume bids. The default minimum is 1.0x planned notional. This is a stale-able snapshot filter, not a fill guarantee or a market-impact model.

A final LONG/SHORT research candidate requires both 1D and 12H direction agreement, a valid 4H setup, a 1H trigger, fresh quotes, a usable order book and a current funding snapshot. Otherwise the engine returns WAIT, NO_TRADE or INSUFFICIENT_DATA with explicit reasons. The score is a transparent heuristic, not a fitted probability.

**Exit plan defaults:** TP1 is 1.0R and closes 50% of the position; TP2 is 2.5R and closes the remaining 50%. This produces 1.75R gross weighted reward if both targets fill, before fees/slippage/funding. The exit fraction and TP2 multiple are configurable. Net RR calculations, Kronos TP/SL races, historical races and backtests must all use the same weighted exit schedule. The first research gate is 1.5 net RR and the default paper risk budget is 0.5%; these are configurable research assumptions, not advice or proof of profitability.
