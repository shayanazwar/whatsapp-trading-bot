V11 START HERE

1. Read V11_STRATEGY_SPEC.md.
2. Use app/analysis/engine.py as the V11 strategy engine.
3. Keep analysis strictly to 1D / 12H / 4H / 1H.
4. 12H is causally built from three contiguous completed 4H candles.
5. Baseline execution is next 1H open.
6. Do not add a score threshold or indicator stack until realized data supports calibration.
7. Run 1D and 7D as smoke tests. Use 30D/90D/180D/365D for strategy evaluation.
