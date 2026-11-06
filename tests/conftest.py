from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_path=str(tmp_path / "state.sqlite3"),
        monitor_enabled=False,
        warmup_candles=120,
        max_margin_usdt=0.50,
        model_leverage=200,
        backtest_tp_pcts=(0.50, 0.75, 1.00),
        backtest_taker_fee_rate=0.0008,
        backtest_slippage_ticks=1,
        mexc_max_retries=0,
        mexc_base_url="https://mexc.test",
    )
