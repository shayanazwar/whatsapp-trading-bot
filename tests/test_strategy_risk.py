from datetime import datetime, timezone

import pandas as pd

from rslr.entries import donchian_breakout
from rslr.models import Contract
from rslr.risk import size_position


def test_donchian_upper_excludes_current_signal_candle():
    rows = []
    for i in range(25):
        high = 101 + (i % 3)
        close = 100 + (i % 3)
        rows.append({"timestamp": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(hours=i), "open": close - .5, "high": high, "low": close - 1, "close": close, "volume": 100})
    rows[-1].update({"open": 104, "high": 108, "low": 103, "close": 107})
    candidate = donchian_breakout(pd.DataFrame(rows), lookback=20)
    assert candidate is not None
    assert candidate.side == "LONG"


def test_position_size_respects_risk_budget_and_exchange_step():
    contract = Contract("BTC_USDT", "BTC", "USDT", 0.001, 0.1, 1, 1, 0.0001, 50)
    result = size_position(
        side="LONG", entry=100, stop=99, equity=100, contract=contract,
        risk_fraction=0.005, taker_fee_rate=0.0002, slippage_rate=0.0001,
        leverage=2, max_leverage=5, max_margin_fraction=0.25,
    )
    assert result.accepted
    assert result.contracts >= 1
    assert result.risk_amount_usd <= 0.500001
    assert result.gross_rr == 1.0


def test_minimum_quantity_that_breaks_risk_is_rejected():
    contract = Contract("BTC_USDT", "BTC", "USDT", 1.0, 0.1, 1, 1, 0.001, 50)
    result = size_position(side="LONG", entry=100, stop=99, equity=100, contract=contract)
    assert not result.accepted


def test_stop_must_be_on_valid_side():
    contract = Contract("BTC_USDT", "BTC", "USDT", 0.001, 0.1, 1, 1, 0.0001, 50)
    result = size_position(side="LONG", entry=100, stop=101, equity=100, contract=contract)
    assert not result.accepted
