# Kronos inference

The app pins upstream Kronos source and model/tokenizer revisions in configuration. `scripts/install_kronos.py` clones the source at the exact commit; `scripts/download_models.py` downloads pinned Hugging Face snapshots. No model weights are included in the source archive.

The standard upstream predictor averages the sample axis after decoding. This app uses a narrowly scoped inference adapter based on upstream autoregressive decoding and intentionally omits the sample-axis mean. Each chunk contains independent sample rows and is decoded into individual OHLCVA paths. The expected internal result is `[samples, horizon, 6]` for one symbol.

The adapter validates shape and finite values. OHLC high/low inconsistencies are repaired conservatively and counted; non-positive OHLC values or non-finite tensors fail the forecast. Volume/amount below zero are clamped and recorded. No forecast is synthesized if the model is unavailable.

Kronos-small context limit is 512. Default forecast uses the last 512 completed 1H candles and 24 future 1H steps. A 48-step horizon is allowed as a research configuration, but accuracy at longer horizons must be measured separately. The seed, model/tokenizer revisions, device, timestamps, temperature, top-p and input fingerprint are recorded.

The 1,000 sample outputs are not 1,000 independent draws from the real-world conditional price distribution. Display them as model-generated scenario paths and path shares, not true probabilities. CPU performance must be benchmarked on the actual Render plan before selecting the service size.
