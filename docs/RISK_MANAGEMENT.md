# Risk and paper sizing

Position risk is budgeted from paper equity multiplied by the configured risk percentage. Contract quantity is rounded down to the exchange volume unit using the contract size. Notional and initial margin are estimated separately; leverage changes collateral required, not the price loss between entry and stop.

Net RR accounts for estimated round-trip taker fees and slippage. It does not guarantee a fill at the stop. Market gaps, liquidity gaps, funding, partial fills and maintenance-margin changes can cause realized losses to differ. The current release reports margin/liquidation estimates from available contract metadata but does not reproduce MEXC's full account-specific cross-margin liquidation engine.

All account values are paper configuration. No private account API or real order placement is implemented. Default paper risk is 0.5% per research candidate and a 3% aggregate planned-risk cap; users must configure conservative limits appropriate to their own simulation assumptions.
