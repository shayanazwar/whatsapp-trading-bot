from __future__ import annotations

import importlib
import hashlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import get_settings
from app.market.candles import validate_candle_continuity

log = logging.getLogger(__name__)
FEATURES = ["open", "high", "low", "close", "volume", "amount"]


@dataclass
class ForecastOutput:
    paths: np.ndarray  # [samples, horizon, 6]
    summary: dict
    metadata: dict


class KronosNotReady(RuntimeError):
    pass


def _repo_path() -> Path:
    return Path(__file__).resolve().parents[2] / "third_party" / "Kronos"


def _time_features(ts: pd.Series) -> np.ndarray:
    dt = pd.to_datetime(ts, utc=True)
    return pd.DataFrame({
        "minute": dt.dt.minute,
        "hour": dt.dt.hour,
        "weekday": dt.dt.weekday,
        "day": dt.dt.day,
        "month": dt.dt.month,
    }).to_numpy(dtype=np.float32)


def _validate_and_repair(paths: np.ndarray, max_invalid_fraction: float = 0.005) -> tuple[np.ndarray, dict]:
    result = np.asarray(paths, dtype=np.float32).copy()
    if result.ndim != 3 or result.shape[-1] != 6:
        raise ValueError("Kronos returned an unexpected tensor shape")
    if not np.isfinite(result).all():
        raise ValueError("Kronos returned NaN or infinite forecast values")
    if (result[..., :4] <= 0).any():
        raise ValueError("Kronos generated non-positive OHLC values; refusing forecast")
    op, hi, lo, cl = (result[..., i] for i in range(4))
    high_floor = np.maximum.reduce([op, cl, lo])
    low_ceil = np.minimum.reduce([op, cl, hi])
    repairs_high = int(np.count_nonzero(hi < high_floor))
    repairs_low = int(np.count_nonzero(lo > low_ceil))
    negative_volume = int(np.count_nonzero(result[..., 4] < 0))
    negative_amount = int(np.count_nonzero(result[..., 5] < 0))
    invalid_fields = repairs_high + repairs_low + negative_volume + negative_amount
    invalid_bar_mask = (
        (hi < high_floor) | (lo > low_ceil) |
        (result[..., 4] < 0) | (result[..., 5] < 0)
    )
    invalid_bar_count = int(np.count_nonzero(invalid_bar_mask))
    total_bars = int(result.shape[0] * result.shape[1])
    invalid_bar_fraction = invalid_bar_count / max(total_bars, 1)
    invalid_field_fraction = invalid_fields / max(total_bars * 6, 1)
    if invalid_bar_fraction > max_invalid_fraction:
        raise ValueError(
            f"Kronos forecast has {invalid_bar_count}/{total_bars} invalid OHLCVA bars "
            f"({invalid_bar_fraction:.4%}), above threshold {max_invalid_fraction:.4%}; forecast rejected"
        )
    # Only small, measured numerical inconsistencies are repaired. Counts and
    # fractions are persisted so downstream consumers cannot mistake repair for raw output.
    result[..., 1] = np.maximum(hi, high_floor)
    result[..., 2] = np.minimum(lo, low_ceil)
    result[..., 4] = np.maximum(result[..., 4], 0)
    result[..., 5] = np.maximum(result[..., 5], 0)
    return result, {
        "high_repairs": repairs_high,
        "low_repairs": repairs_low,
        "negative_volume_clamped": negative_volume,
        "negative_amount_clamped": negative_amount,
        "invalid_field_count": invalid_fields,
        "invalid_field_fraction": invalid_field_fraction,
        "invalid_bar_count": invalid_bar_count,
        "invalid_bar_fraction": invalid_bar_fraction,
        "max_invalid_bar_fraction": max_invalid_fraction,
    }


def summarize_forecast_paths(paths: np.ndarray, historical_closes: np.ndarray | list[float]) -> dict:
    """Summarize real decoded Kronos paths without presenting path share as probability.

    Direction is based on the median terminal sample return. Volatility outlook is a
    transparent heuristic comparing path dispersion with recent historical return
    volatility; it is not a calibrated forecast probability.
    """
    paths = np.asarray(paths, dtype=np.float64)
    closes = np.asarray(historical_closes, dtype=np.float64).reshape(-1)
    if paths.ndim != 3 or paths.shape[0] < 1 or paths.shape[1] < 1 or paths.shape[2] < 4:
        raise ValueError("Forecast paths must have shape [samples, horizon, >=4]")
    if closes.size < 2 or not np.isfinite(closes).all() or (closes <= 0).any():
        raise ValueError("At least two finite positive historical closes are required")
    if not np.isfinite(paths[..., :4]).all() or (paths[..., :4] <= 0).any():
        raise ValueError("Forecast paths contain invalid OHLC values")
    last_close = float(closes[-1])
    final_close = paths[:, -1, 3]
    returns = final_close / last_close - 1.0
    median_return = float(np.median(returns))
    expected_close = float(np.median(final_close))
    median_ohlc = np.median(paths[..., :4], axis=0)
    median_ohlc[:, 1] = np.maximum(median_ohlc[:, 1], np.maximum(median_ohlc[:, 0], median_ohlc[:, 3]))
    median_ohlc[:, 2] = np.minimum(median_ohlc[:, 2], np.minimum(median_ohlc[:, 0], median_ohlc[:, 3]))
    anchored_prices = np.concatenate([np.full((paths.shape[0], 1), last_close), paths[:, :, 3]], axis=1)
    path_log_returns = np.diff(np.log(anchored_prices), axis=1)
    horizon_scale = np.sqrt(max(paths.shape[1], 1))
    path_volatility_pct = np.std(path_log_returns, axis=1) * horizon_scale * 100.0
    hist_log_returns = np.diff(np.log(closes[-min(len(closes), 97):]))
    historical_volatility_pct = float(np.std(hist_log_returns) * horizon_scale * 100.0)
    forecast_volatility_pct = float(np.median(path_volatility_pct))
    vol_ratio = forecast_volatility_pct / max(historical_volatility_pct, 1e-9)
    if vol_ratio < 0.8:
        volatility_outlook = "LOW"
    elif vol_ratio > 1.25:
        volatility_outlook = "HIGH"
    else:
        volatility_outlook = "NORMAL"
    if median_return > 0.001:
        direction = "BULLISH"
    elif median_return < -0.001:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"
    return {
        "samples": int(paths.shape[0]),
        "horizon": int(paths.shape[1]),
        "baseline_close": last_close,
        "expected_close": expected_close,
        "expected_return_pct": median_return * 100.0,
        "forecast_direction": direction,
        "bullish_path_share": float(np.mean(final_close > last_close)),
        "bearish_path_share": float(np.mean(final_close < last_close)),
        "neutral_path_share": float(np.mean(final_close == last_close)),
        "return_quantiles": {f"p{q}": float(np.quantile(returns, q / 100)) for q in [5, 10, 25, 50, 75, 90, 95]},
        "close_quantiles_by_step": {
            f"p{q}": np.quantile(paths[:, :, 3], q / 100, axis=0).astype(float).tolist()
            for q in [10, 25, 50, 75, 90]
        },
        "high_quantiles_by_step": np.quantile(paths[:, :, 1], [0.1, 0.5, 0.9], axis=0).astype(float).tolist(),
        "low_quantiles_by_step": np.quantile(paths[:, :, 2], [0.1, 0.5, 0.9], axis=0).astype(float).tolist(),
        "median_ohlc_by_step": median_ohlc.astype(float).tolist(),
        "uncertainty_width_pct": float((np.quantile(final_close, 0.90) - np.quantile(final_close, 0.10)) / last_close * 100.0),
        "forecast_volatility_pct": forecast_volatility_pct,
        "historical_comparable_volatility_pct": historical_volatility_pct,
        "volatility_outlook": volatility_outlook,
        "volatility_outlook_method": "median_path_realized_volatility_vs_recent_historical_volatility; heuristic_not_calibrated",
        "calibration_status": "UNVALIDATED_PATH_SHARE_NOT_MARKET_PROBABILITY",
    }


def _individual_sample_inference(tokenizer, model, x, x_stamp, y_stamp, max_context: int,
                                 pred_len: int, temperature: float, top_p: float):
    """Decode individual Kronos paths instead of upstream's default sample-axis mean.

    This is a narrowly scoped adaptation of the MIT-licensed upstream
    `auto_regressive_inference` implementation. See THIRD_PARTY_NOTICES.md.
    All model/tokenizer layers remain the official upstream implementation.
    """
    import torch

    kronos_mod = importlib.import_module("model.kronos")
    sampler = kronos_mod.sample_from_logits
    with torch.no_grad():
        x = torch.clip(x, -5, 5)
        initial_seq_len = x.size(1)
        batch_size = x.size(0)
        total_seq_len = initial_seq_len + pred_len
        full_stamp = torch.cat([x_stamp, y_stamp], dim=1)
        x_token = tokenizer.encode(x, half=True)
        generated_pre = x_token[0].new_empty(batch_size, pred_len)
        generated_post = x_token[1].new_empty(batch_size, pred_len)
        pre_buffer = x_token[0].new_zeros(batch_size, max_context)
        post_buffer = x_token[1].new_zeros(batch_size, max_context)
        buffer_len = min(initial_seq_len, max_context)
        if buffer_len:
            start_idx = max(0, initial_seq_len - max_context)
            pre_buffer[:, :buffer_len] = x_token[0][:, start_idx:start_idx + buffer_len]
            post_buffer[:, :buffer_len] = x_token[1][:, start_idx:start_idx + buffer_len]
        for i in range(pred_len):
            current_seq_len = initial_seq_len + i
            window_len = min(current_seq_len, max_context)
            if current_seq_len <= max_context:
                input_tokens = [pre_buffer[:, :window_len], post_buffer[:, :window_len]]
            else:
                input_tokens = [pre_buffer, post_buffer]
            context_start = max(0, current_seq_len - max_context)
            current_stamp = full_stamp[:, context_start:current_seq_len, :].contiguous()
            s1_logits, context = model.decode_s1(input_tokens[0], input_tokens[1], current_stamp)
            sample_pre = sampler(s1_logits[:, -1, :], temperature=temperature, top_k=0,
                                 top_p=top_p, sample_logits=True)
            s2_logits = model.decode_s2(context, sample_pre)
            sample_post = sampler(s2_logits[:, -1, :], temperature=temperature, top_k=0,
                                  top_p=top_p, sample_logits=True)
            generated_pre[:, i] = sample_pre.squeeze(-1)
            generated_post[:, i] = sample_post.squeeze(-1)
            if current_seq_len < max_context:
                pre_buffer[:, current_seq_len] = sample_pre.squeeze(-1)
                post_buffer[:, current_seq_len] = sample_post.squeeze(-1)
            else:
                pre_buffer.copy_(torch.roll(pre_buffer, shifts=-1, dims=1))
                post_buffer.copy_(torch.roll(post_buffer, shifts=-1, dims=1))
                pre_buffer[:, -1] = sample_pre.squeeze(-1)
                post_buffer[:, -1] = sample_post.squeeze(-1)
        full_pre = torch.cat([x_token[0], generated_pre], dim=1)
        full_post = torch.cat([x_token[1], generated_post], dim=1)
        context_start = max(0, total_seq_len - max_context)
        decoded = tokenizer.decode([
            full_pre[:, context_start:total_seq_len].contiguous(),
            full_post[:, context_start:total_seq_len].contiguous(),
        ], half=True)
        return decoded[:, -pred_len:, :]


class KronosForecastEngine:
    def __init__(self):
        self.settings = get_settings()
        self.model = None
        self.tokenizer = None
        self.device = None
        self._loaded = False

    def load(self):
        if self._loaded:
            return
        repo = _repo_path()
        if not (repo / "model" / "kronos.py").exists():
            raise KronosNotReady(
                "Pinned Kronos source is missing. Run `python scripts/install_kronos.py` and install `requirements-inference.txt`."
            )
        sys.path.insert(0, str(repo))
        try:
            import torch
            from model import Kronos, KronosTokenizer
        except Exception as exc:
            raise KronosNotReady(f"Kronos/PyTorch dependencies are unavailable: {exc}") from exc
        if self.settings.kronos_device == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
        else:
            device = self.settings.kronos_device
            if device.startswith("cuda") and not torch.cuda.is_available():
                raise KronosNotReady("CUDA requested but no CUDA device is available")
        self.tokenizer = KronosTokenizer.from_pretrained(
            self.settings.kronos_tokenizer_id,
            revision=self.settings.kronos_tokenizer_revision,
        )
        self.model = Kronos.from_pretrained(
            self.settings.kronos_model_id,
            revision=self.settings.kronos_model_revision,
        )
        self.tokenizer = self.tokenizer.to(device)
        self.model = self.model.to(device).eval()
        self.device = device
        self._loaded = True
        log.info("Kronos loaded model=%s tokenizer=%s device=%s", self.settings.kronos_model_id,
                 self.settings.kronos_tokenizer_id, device)

    def forecast(self, candles: pd.DataFrame, *, samples: int | None = None,
                 horizon: int | None = None, seed: int | None = None,
                 timeframe_seconds: int = 3600) -> ForecastOutput:
        self.load()
        import torch

        samples = self.settings.forecast_sample_count if samples is None else int(samples)
        horizon = self.settings.forecast_horizon if horizon is None else int(horizon)
        seed = self.settings.forecast_random_seed if seed is None else seed
        if samples < 1 or horizon < 1 or horizon > 48:
            raise ValueError("samples must be positive and horizon must be between 1 and 48")
        if not isinstance(candles, pd.DataFrame):
            raise ValueError("candles must be a DataFrame")
        # Reject missing feature columns, duplicate timestamps and every gap before
        # trimming to context. Kronos must never interpret a missing hour as one step.
        frame = validate_candle_continuity(candles, timeframe_seconds)
        frame = frame.tail(self.settings.kronos_context_length).reset_index(drop=True)
        if len(frame) < 64:
            raise ValueError("At least 64 completed historical candles are required")
        missing = set(["open", "high", "low", "close", "volume", "amount"]) - set(frame.columns)
        if missing:
            raise ValueError(f"Kronos input is missing required OHLCVA fields: {sorted(missing)}")
        x_ts = pd.to_datetime(frame["timestamp"], utc=True)
        step_seconds = int((x_ts.iloc[-1] - x_ts.iloc[-2]).total_seconds())
        if step_seconds <= 0:
            raise ValueError("Candle timestamps must be strictly increasing")
        y_ts = pd.Series(pd.date_range(x_ts.iloc[-1] + pd.Timedelta(seconds=step_seconds),
                                       periods=horizon, freq=pd.Timedelta(seconds=step_seconds)))
        x_raw = frame[FEATURES].to_numpy(dtype=np.float32)
        x_mean = x_raw.mean(axis=0)
        x_std = x_raw.std(axis=0)
        x_scale = x_std + 1e-5
        normalized = np.clip((x_raw - x_mean) / x_scale, -5, 5)
        x_stamp = _time_features(x_ts)
        y_stamp = _time_features(y_ts)
        chunk_size = max(1, min(self.settings.forecast_chunk_size, samples))
        all_paths = []
        with torch.inference_mode():
            for chunk_number, start in enumerate(range(0, samples, chunk_size)):
                chunk_count = min(chunk_size, samples - start)
                local_seed = int(seed + chunk_number)
                torch.manual_seed(local_seed)
                if torch.cuda.is_available():
                    torch.cuda.manual_seed_all(local_seed)
                x = torch.from_numpy(normalized).unsqueeze(0).repeat(chunk_count, 1, 1).to(self.device)
                xs = torch.from_numpy(x_stamp).unsqueeze(0).repeat(chunk_count, 1, 1).to(self.device)
                ys = torch.from_numpy(y_stamp).unsqueeze(0).repeat(chunk_count, 1, 1).to(self.device)
                decoded = _individual_sample_inference(
                    self.tokenizer, self.model, x, xs, ys,
                    self.settings.kronos_context_length, horizon,
                    self.settings.forecast_temperature, self.settings.forecast_top_p,
                )
                chunk = decoded.detach().cpu().numpy().astype(np.float32)
                chunk = chunk * x_scale[None, None, :] + x_mean[None, None, :]
                all_paths.append(chunk)
        paths, repair_counts = _validate_and_repair(
            np.concatenate(all_paths, axis=0),
            max_invalid_fraction=self.settings.forecast_max_invalid_bar_fraction,
        )
        if paths.shape != (samples, horizon, 6):
            raise RuntimeError(f"Expected {(samples, horizon, 6)}, got {paths.shape}")
        summary = summarize_forecast_paths(paths, frame["close"].to_numpy())
        summary["ohlc_repairs"] = repair_counts
        metadata = {
            "model_id": self.settings.kronos_model_id,
            "model_revision": self.settings.kronos_model_revision,
            "tokenizer_id": self.settings.kronos_tokenizer_id,
            "tokenizer_revision": self.settings.kronos_tokenizer_revision,
            "device": self.device,
            "seed": seed,
            "temperature": self.settings.forecast_temperature,
            "top_p": self.settings.forecast_top_p,
            "input_candles": len(frame),
            "input_end_time": x_ts.iloc[-1].isoformat(),
            "baseline_models": {
                "naive_last_value": float(frame["close"].iloc[-1]),
                "drift_mean_log_return_per_bar": float(np.mean(np.diff(np.log(frame["close"].tail(25).to_numpy(dtype=float))))),
                "drift_lookback_bars": min(24, len(frame) - 1),
            },
            "prediction_timestamps": [x.isoformat() for x in y_ts],
            "individual_paths_preserved": True,
            "timeframe_seconds": timeframe_seconds,
            "input_data_sha256": hashlib.sha256(
                pd.util.hash_pandas_object(frame[["timestamp"] + FEATURES], index=False).values.tobytes()
            ).hexdigest(),
            "forecast_validation": "PASSED_WITH_RECORDED_REPAIRS" if repair_counts["invalid_field_count"] else "PASSED_RAW",
            # The upstream release material does not provide an independently
            # verifiable exact pretraining-data cutoff. Historical tests therefore
            # cannot rule out overlap with the model's pretraining corpus.
            "pretraining_cutoff_verified": False,
            "temporal_leakage_assessment": "POTENTIAL_PRETRAINING_OVERLAP_NOT_EXCLUDABLE",
        }
        return ForecastOutput(paths=paths, summary=summary, metadata=metadata)
