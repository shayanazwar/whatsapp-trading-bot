from __future__ import annotations

from dataclasses import replace

import pandas as pd

from rslr.config import settings
from rslr.data.candles import build_timeframes, validate_candles
from rslr.entries import EntryCandidate, donchian_breakout, sweep_reclaim
from rslr.intelligence import (
    BIAS_BOTH,
    BIAS_LONG,
    BIAS_NONE,
    BIAS_SHORT,
    btc_regime,
    funding_allows_side,
    funding_context,
    funding_is_usable,
    four_hour_context,
    relative_strength,
)
from rslr.models import Contract, TradeSignal
from rslr.risk import size_position


def _completed(frame: pd.DataFrame, interval_hours: int, asof: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[frame.timestamp + pd.Timedelta(hours=interval_hours) <= asof].reset_index(drop=True)


class SignalEngine:
    def __init__(self) -> None:
        self.enable_sweep = settings.enable_sweep
        self.enable_donchian = settings.enable_donchian

    def generate_for_symbol(
        self,
        *,
        symbol: str,
        symbol_1h: pd.DataFrame,
        btc_1h: pd.DataFrame,
        funding: pd.DataFrame,
        contract: Contract,
        equity: float | None = None,
        symbol_frames_cache: dict[str, pd.DataFrame] | None = None,
        btc_frames_cache: dict[str, pd.DataFrame] | None = None,
    ) -> list[TradeSignal]:
        """Generate signals using only completed bars, with HTF values available at close."""
        if len(symbol_1h) < 60 or len(btc_1h) < 60:
            return []
        one = validate_candles(symbol_1h, interval_hours=1)
        btc = validate_candles(btc_1h, interval_hours=1)
        asof = pd.Timestamp(one.iloc[-1].timestamp) + pd.Timedelta(hours=1)
        symbol_frames = symbol_frames_cache or build_timeframes(one)
        btc_frames = btc_frames_cache or build_timeframes(btc)
        daily = _completed(btc_frames["1D"], 24, asof)
        symbol_12h = _completed(symbol_frames["12H"], 12, asof)
        btc_12h = _completed(btc_frames["12H"], 12, asof)
        symbol_4h = _completed(symbol_frames["4H"], 4, asof)
        regime = btc_regime(daily)
        rs = relative_strength(symbol_12h, btc_12h)
        current_funding = funding_is_usable(funding, asof)
        context_4h = four_hour_context(symbol_4h)
        context_funding = funding_context(funding, asof)
        if regime == BIAS_NONE or rs is None or current_funding is None:
            return []

        hourly = one.copy()
        current_atr = hourly
        candidates: list[EntryCandidate] = []
        if self.enable_sweep:
            item = sweep_reclaim(current_atr)
            if item:
                candidates.append(item)
        if self.enable_donchian:
            item = donchian_breakout(current_atr)
            if item:
                candidates.append(item)

        signals_by_key: dict[tuple[str, str, str], TradeSignal] = {}
        equity = settings.account_equity_usd if equity is None else equity
        for candidate in candidates:
            if regime == BIAS_LONG and candidate.side != "LONG":
                continue
            if regime == BIAS_SHORT and candidate.side != "SHORT":
                continue
            if regime == BIAS_BOTH and candidate.side == "LONG" and rs <= 0:
                continue
            if regime == BIAS_BOTH and candidate.side == "SHORT" and rs >= 0:
                continue
            if candidate.side == "LONG" and rs <= 0:
                continue
            if candidate.side == "SHORT" and rs >= 0:
                continue
            if not funding_allows_side(candidate.side, current_funding):
                continue
            sized = size_position(
                side=candidate.side,
                entry=candidate.entry,
                stop=candidate.stop,
                equity=equity,
                contract=contract,
                risk_fraction=settings.risk_per_trade,
                taker_fee_rate=settings.taker_fee_rate,
                slippage_rate=settings.slippage_rate,
                funding_rate=current_funding,
                leverage=settings.default_leverage,
                max_leverage=settings.max_leverage,
                max_margin_fraction=settings.max_margin_fraction,
            )
            if not sized.accepted:
                continue
            key = (symbol, candidate.side, candidate.signal_timestamp.isoformat())
            candidate_module = candidate.module
            if key in signals_by_key:
                old = signals_by_key[key]
                modules = {old.module, candidate_module}
                module_name = "SWEEP+DONCHIAN" if len(modules) == 2 else old.module
                signals_by_key[key] = replace(old, module=module_name)
                continue
            signals_by_key[key] = TradeSignal(
                symbol=symbol,
                side=candidate.side,
                module=candidate_module,
                signal_time=candidate.signal_timestamp.to_pydatetime(),
                reference_entry=candidate.entry,
                stop_loss=sized.stop_loss,
                take_profit=sized.take_profit,
                risk_amount_usd=sized.risk_amount_usd,
                risk_pct=sized.risk_pct,
                contracts=sized.contracts,
                leverage=sized.leverage,
                estimated_fees_usd=sized.fees_usd,
                estimated_slippage_usd=sized.slippage_usd,
                estimated_funding_usd=sized.funding_usd,
                gross_rr=sized.gross_rr,
                net_rr=sized.net_rr,
                btc_bias=regime,
                relative_strength_score=rs,
                funding_rate=current_funding,
                metadata={
                    "timeframe": "1H", "signal_close_time": asof.isoformat(),
                    "4h_context": context_4h, "funding_context": context_funding,
                },
            )
        return list(signals_by_key.values())
