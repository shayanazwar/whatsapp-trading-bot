# V11 Accuracy Research: Evidence, Experiments, and Limits

## Research principle

A discretionary concept should only become a live hard gate after it is measurable and survives a controlled out-of-sample test. This patch therefore corrects a causal sequencing issue and adds transparent entry-quality measurements, while keeping the extra quality filters opt-in and disabled by default.

## Source-backed rationale

1. **Pullback/context quality matters, but trend/range transitions are uncertain.** Adam Grimes, *The Art and Science of Technical Analysis*, chapter "Interfaces between Trends and Ranges," describes trend-to-range transitions as uncertain/high-volatility periods and notes that many breakouts fail. This supports recording impulse age, retracement depth, and reclaim quality rather than adding many correlated indicators. Source: https://onlinelibrary.wiley.com/doi/10.1002/9781119202837.ch5
2. **Parameter search can overfit.** Bailey, Borwein, López de Prado, and Zhu, "The Probability of Backtest Overfitting," propose estimating overfitting risk in investment simulations. This supports frozen windows, predeclared settings, and an untouched validation period. Source: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
3. **Crypto momentum is not uniformly stable.** Grobys et al., "Cryptocurrency momentum has (not) its moments" (2025), reports severe momentum crashes and finds volatility management can mitigate some crash exposure; a single asset may undermine portfolio returns. This supports reporting side/symbol/regime diagnostics and retaining portfolio/risk controls. Source: https://link.springer.com/article/10.1007/s11408-025-00474-9
4. **Transaction costs matter and effects vary by cryptocurrency.** Deprez and Frömmel, "Are simple technical trading rules profitable in bitcoin markets?" (2024), studies many rules with realistic trading costs and data-mining controls; an earlier crypto technical-analysis study also finds cost effects vary by coin. Sources: https://www.sciencedirect.com/science/article/pii/S1059056024003010 and https://www.sciencedirect.com/science/article/pii/S1042443122000816

These sources support research discipline and measuring execution/entry conditions. They do **not** establish that the specific values 3 bars, 0.75 ATR, or 70% improve this engine. Those values are experimental defaults only.

## Implemented in this patch

- Corrected sweep/reclaim sequencing so the bar limit is measured from the sweep candle and a current completed-candle reclaim is preferred over an older historical reclaim.
- ADX no longer counts as a directional vote. It measures strength, not direction.
- Accepted signals record impulse age, measured pullback depth, sweep-to-reclaim delay, reclaim-body ratio, close location, relative volume, and entry extension.
- Backtest reports compare available quality averages for TP and SL trades, with sample sizes and an explicit warning that these are diagnostics, not proven predictors.
- `V11_ACCURACY_MODE` offers the more selective entry rules as a controlled experiment, OFF by default.

## Controlled experiment protocol

1. Use identical frozen `V11_BACKTEST_START_MS` and `V11_BACKTEST_END_MS`, candles, universe snapshot, fees, slippage, and portfolio controls.
2. Run the baseline with `V11_ACCURACY_MODE=false`.
3. Run the experiment with `V11_ACCURACY_MODE=true`; do not change other parameters.
4. Compare selected signal IDs as well as net expectancy, profit factor, max drawdown, side results, fee burden, and trade count.
5. Validate on time periods not used to choose thresholds (walk-forward/chronological holdout). Treat a small sample or one-symbol outlier as inconclusive.
6. Keep live/automatic execution disabled while evaluating the experimental mode.

No historical MEXC backtest is claimed by this patch because the archive does not contain the frozen historical candles/universe snapshot. Software tests validate the logic, not predictive profitability.
