import numpy as np
import pytest

from app.artifacts.store import DatabaseForecastArtifactStore


def test_artifact_store_round_trip():
    store = DatabaseForecastArtifactStore()
    paths = np.ones((5, 7, 6), dtype=np.float32)
    paths[:, :, 1] = 1.2
    paths[:, :, 2] = 0.8
    blob = store.encode(paths)
    restored = store.decode(blob, paths.shape)
    assert restored.shape == paths.shape
    assert np.array_equal(restored, paths)


def test_artifact_store_rejects_mismatched_shape():
    store = DatabaseForecastArtifactStore()
    blob = store.encode(np.ones((5, 7, 6), dtype=np.float32))
    with pytest.raises(ValueError):
        store.decode(blob, (4, 7, 6))
