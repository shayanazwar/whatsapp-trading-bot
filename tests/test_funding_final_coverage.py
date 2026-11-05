from app.services.command_processor import _final_funding_coverage


def test_final_funding_coverage_includes_positions_created_after_preflight():
    coverage, missing, status = _final_funding_coverage(
        {"BTC_USDT": True},
        [{"symbol": "BTC_USDT"}, {"symbol": "ETH_USDT"}],
        [{"symbol": "SOL_USDT"}],
    )
    assert coverage == {"BTC_USDT": True, "ETH_USDT": False, "SOL_USDT": False}
    assert missing == ["ETH_USDT", "SOL_USDT"]
    assert status == "INCOMPLETE"


def test_final_funding_coverage_is_not_required_without_positions():
    coverage, missing, status = _final_funding_coverage({}, [], [])
    assert coverage == {}
    assert missing == []
    assert status == "NOT_REQUIRED"


def test_final_funding_coverage_complete_for_every_final_position():
    coverage, missing, status = _final_funding_coverage(
        {"BTC_USDT": True, "ETH_USDT": True},
        [{"symbol": "BTC_USDT"}],
        [{"symbol": "ETH_USDT"}],
    )
    assert missing == []
    assert status == "COMPLETE"
    assert set(coverage) == {"BTC_USDT", "ETH_USDT"}
