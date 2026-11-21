import importlib.util
import json
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from services.anomaly_detection import LSTMAutoencoderDetector

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_SCRIPT_PATH = REPO_ROOT / "scripts" / "run_anomaly_benchmark.py"


def _load_benchmark_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_anomaly_benchmark", BENCHMARK_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic_normal_series(n: int = 240, n_features: int = 7, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.linspace(0.0, 8.0 * np.pi, n)
    cols = [np.sin(t + 0.4 * f) + rng.normal(0.0, 0.08, size=n) for f in range(n_features)]
    return np.column_stack(cols).astype(np.float64)


class TestLSTMAutoencoderDetector:
    def test_unfitted_and_invalid_input_errors(self) -> None:
        x = _synthetic_normal_series(n=80)
        det = LSTMAutoencoderDetector(window_size=10, epochs=2, batch_size=16)

        with pytest.raises(RuntimeError):
            det.score(x)
        with pytest.raises(RuntimeError):
            det.predict(x)
        with pytest.raises(RuntimeError):
            det.calibrate_threshold(x, np.zeros(len(x), dtype=np.int64))

        det.fit(x)
        with pytest.raises(RuntimeError, match="threshold is not set"):
            det.predict(x)
        with pytest.raises(ValueError, match="Expected 7 features"):
            det.score(np.zeros((20, 4), dtype=np.float64))

        bad = x[:15].copy()
        bad[3, 2] = np.nan
        with pytest.raises(ValueError, match="NaN or infinite"):
            det.score(bad)

        with pytest.raises(ValueError, match="context_prefix"):
            det.score(x[:15], context_prefix=x[:3])

    def test_training_converges_and_flags_injected_spikes(self) -> None:
        x_train = _synthetic_normal_series(n=300, seed=7)
        x_eval = _synthetic_normal_series(n=120, seed=99)
        x_eval[70:85] += 5.0

        det = LSTMAutoencoderDetector(
            window_size=12,
            hidden_size=16,
            latent_size=8,
            epochs=10,
            batch_size=32,
            learning_rate=5e-3,
            patience=10,
            random_state=42,
        ).fit(x_train)

        assert len(det.train_losses_) >= 2
        assert det.train_losses_[-1] < det.train_losses_[0]

        scores = det.score(x_eval, context_prefix=x_train[-11:])
        assert scores.shape == (len(x_eval),)
        assert np.all(np.isfinite(scores))

        normal_mean = float(np.mean(scores[20:60]))
        spike_mean = float(np.mean(scores[70:85]))
        assert spike_mean > normal_mean * 3.0

    def test_strict_causality_no_future_lookahead(self) -> None:
        x_train = _synthetic_normal_series(n=160, seed=11)
        x_eval = _synthetic_normal_series(n=60, seed=12)

        det = LSTMAutoencoderDetector(
            window_size=10,
            hidden_size=16,
            latent_size=8,
            epochs=2,
            batch_size=32,
            random_state=42,
        ).fit(x_train)

        base_no_prefix = det.score(x_eval)
        base_with_prefix = det.score(x_eval, context_prefix=x_train[-9:])

        for t in (0, 5, 9, 25, 58):
            mutated = x_eval.copy()
            mutated[t + 1] += 500.0

            mut_no_prefix = det.score(mutated)
            mut_with_prefix = det.score(mutated, context_prefix=x_train[-9:])

            np.testing.assert_allclose(
                mut_no_prefix[: t + 1], base_no_prefix[: t + 1], rtol=0.0, atol=1e-7
            )
            np.testing.assert_allclose(
                mut_with_prefix[: t + 1], base_with_prefix[: t + 1], rtol=0.0, atol=1e-7
            )

    def test_save_and_load_roundtrip_weights_only(self, tmp_path: Path) -> None:
        x_train = _synthetic_normal_series(n=160, seed=3)
        x_val = _synthetic_normal_series(n=60, seed=4)
        y_val = np.zeros(60, dtype=np.int64)
        x_val[35:45] += 5.0
        y_val[35:45] = 1

        det = LSTMAutoencoderDetector(
            window_size=10,
            hidden_size=16,
            latent_size=8,
            epochs=3,
            batch_size=32,
            random_state=42,
        ).fit(x_train)
        det.calibrate_threshold(x_val, y_val, context_prefix=x_train[-9:])

        expected_scores = det.score(x_val, context_prefix=x_train[-9:])
        expected_preds = det.predict(x_val, context_prefix=x_train[-9:])

        artifact_dir = tmp_path / "lstm_model"
        det.save(artifact_dir)
        assert (artifact_dir / "config.json").is_file()
        assert (artifact_dir / "weights.pt").is_file()

        loaded = LSTMAutoencoderDetector.load(artifact_dir)
        assert loaded.threshold == pytest.approx(det.threshold)
        assert loaded.window_size == 10

        actual_scores = loaded.score(x_val, context_prefix=x_train[-9:])
        actual_preds = loaded.predict(x_val, context_prefix=x_train[-9:])
        np.testing.assert_allclose(actual_scores, expected_scores, rtol=0.0, atol=1e-7)
        np.testing.assert_array_equal(actual_preds, expected_preds)


def test_three_way_benchmark_smoke(tmp_path: Path) -> None:
    mod = _load_benchmark_module()
    results = mod.run_benchmark(
        tmp_path / "bench",
        seed=42,
        days=2,
        freq_minutes=2,
        window_size=15,
        lstm_epochs=2,
        save_artifacts=True,
    )

    results_file = tmp_path / "bench" / "results.json"
    assert results_file.is_file()
    persisted = json.loads(results_file.read_text(encoding="utf-8"))
    assert persisted["synthetic"] is True
    assert set(persisted["detectors"]) == {
        "rolling_zscore",
        "isolation_forest",
        "lstm_autoencoder",
    }

    for name, metrics in results["detectors"].items():
        assert 0.0 <= metrics["precision"] <= 1.0, name
        assert 0.0 <= metrics["recall"] <= 1.0, name
        assert 0.0 <= metrics["f1"] <= 1.0, name
        assert 0.0 <= metrics["pr_auc"] <= 1.0, name
        assert metrics["total_fault_events"] == 5, name
        assert np.isfinite(metrics["fit_seconds"]), name
        assert np.isfinite(metrics["score_seconds"]), name
        assert np.isfinite(metrics["false_positives_per_hour"]), name
