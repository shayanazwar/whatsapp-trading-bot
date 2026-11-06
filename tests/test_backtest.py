from __future__ import annotations

import asyncio

from app.backtest import _run_variant, run_backtest
from app.models import Candle, ContractInfo
from app.mexc import CandleBatch
from app.strategy import StrategyConfig


def contract():
    return ContractInfo("BTC_USDT", 0.0001, 0.1, 1, 1, 400000, 500, 0, 0.0008, 0.001, 0.0004)


def test_backtest_models_tp_touch_and_costs(settings):
    candles = [
        Candle(0, 99, 100, 98, 99, 1),
        Candle(60, 111, 113, 110, 112, 1),
        Candle(120, 112, 114, 111, 113, 1),
        Candle(180, 113, 115, 112, 114, 1),
    ]
    cfg = StrategyConfig(6, 12, 21, 9, 65, 35, 0.5)
    result = _run_variant(
        candles, 1, "BOTH", cfg, 0.5, contract(), settings,
        [None, 108, 109, 110], [None, 105, 106, 107], [None, 102, 103, 104], [65, 66, 67, 68],
    )
    assert result.setups == 1
    assert result.long_entries == 1
    assert result.tp_touches == 1
    assert result.unresolved == 0
    assert result.fees > 0
    assert result.tp_subset_net < result.tp_subset_gross


class FakeMexc:
    def __init__(self, candles):
        self.candles = candles

    async def get_candle_batch(self, start, end):
        return CandleBatch(self.candles, start, end, len(self.candles), (), (), (), (), ())

    async def get_contract_info(self):
        return contract()


def test_run_backtest_produces_all_12_variants_without_fake_metrics(settings):
    now = 1_800_000_000
    end = ((now - 1) // 60) * 60 - 60
    test_bars = 1440
    test_start = end - (test_bars - 1) * 60
    start = test_start - settings.warmup_candles * 60
    candles = [Candle(t, 100, 100.1, 99.9, 100, 0) for t in range(start, end + 1, 60)]
    output = asyncio.run(run_backtest(FakeMexc(candles), settings, 1, "BOTH", now))
    assert "Candles downloaded: 1560" in output
    assert output.count("RSI ") == 12
    assert "Liquidation/funding NOT MODELLED" in output
    assert "Win rate:" not in output
    assert "Profit Factor" not in output


def test_backtest_skips_stale_next_open_signal(settings):
    candles = [
        Candle(0, 99, 100, 98, 99, 1),
        Candle(60, 111, 113, 110, 112, 1),
        Candle(120, 114, 116, 113, 115, 1),
    ]
    cfg = StrategyConfig(6, 12, 21, 9, 65, 35, 0.5)
    result = _run_variant(
        candles, 1, "BOTH", cfg, 0.5, contract(), settings,
        [None, 108, 109], [None, 105, 106], [None, 102, 103], [65, 66, 67],
    )
    assert result.setups == 1
    assert result.stale_rejected == 1
    assert result.long_entries == 0
