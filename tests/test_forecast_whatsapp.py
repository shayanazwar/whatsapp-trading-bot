import numpy as np
import pytest
from app.forecasting.kronos import _validate_and_repair, KronosForecastEngine, KronosNotReady
from app.notifications.whatsapp import WhatsAppSignalAdapter, WhatsAppPolicyBlocked


def test_forecast_validation_and_repair():
    paths = np.array([
        [[100, 99, 101, 102, -1, 1]],
        [[100, 102, 98, 101, 10, 1000]],
    ], dtype=np.float32)
    # One of two bars is malformed; bounded bar-level repair is allowed at 75%.
    fixed, counts = _validate_and_repair(paths, max_invalid_fraction=0.75)
    assert fixed[0,0,1] >= 102
    assert fixed[0,0,2] <= 100
    assert fixed[0,0,4] == 0
    assert counts["high_repairs"] == 1
    assert counts["low_repairs"] == 1
    assert counts["negative_volume_clamped"] == 1
    assert counts["invalid_bar_count"] == 1
    assert counts["invalid_bar_fraction"] == pytest.approx(0.5)


def test_forecast_rejects_nonfinite():
    with pytest.raises(ValueError, match="NaN"):
        _validate_and_repair(np.array([[[1, 1, 1, np.nan, 1, 1]]]))


def test_forecast_rejects_nonpositive_price():
    with pytest.raises(ValueError, match="non-positive"):
        _validate_and_repair(np.array([[[0, 1, 0, 1, 1, 1]]]))


def test_model_missing_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr("app.forecasting.kronos._repo_path", lambda: tmp_path)
    engine = KronosForecastEngine()
    with pytest.raises(KronosNotReady, match="source is missing"):
        engine.load()
