from pathlib import Path

import numpy as np
import pytest
from sklearn.metrics import average_precision_score

from services.anomaly_detection import (
    IsolationForestDetector,
    MultivariatePreprocessor,
    RollingZScoreDetector,
    calibrate_threshold,
    evaluate_predictions,
    generate_benchmark_dataset,
    split_chronological,
)


class TestEvaluationMetrics:
    def test_evaluate_predictions_hand_calculated(self) -> None:
        # 12 samples at 5-minute spacing:
        # Normal points: indices 0, 1, 5, 6, 9 (5 points = 25/60 hours)
        # Fault runs: [2..4], [7..8], [10..11] (3 contiguous faults)
        y_true = np.array([0, 0, 1, 1, 1, 0, 0, 1, 1, 0, 1, 1], dtype=np.int64)
        scores = np.array(
            [0.1, 0.9, 0.2, 0.8, 0.7, 0.1, 0.1, 0.3, 0.2, 0.1, 0.95, 0.85],
            dtype=np.float64,
        )
        threshold = 0.5

        res = evaluate_predictions(y_true, scores, threshold, freq_minutes=5.0)

        assert res.true_positives == 4
        assert res.false_positives == 1
        assert res.true_negatives == 4
        assert res.false_negatives == 3

        assert res.precision == pytest.approx(4 / 5)
        assert res.recall == pytest.approx(4 / 7)
        assert res.f1 == pytest.approx(8 / 12)
        assert res.pr_auc == pytest.approx(float(average_precision_score(y_true, scores)))
        assert res.threshold == pytest.approx(0.5)

        assert res.total_fault_events == 3
        assert res.detected_fault_events == 2
        assert res.per_fault_delays_minutes == [5.0, None, 0.0]
        assert res.mean_detection_delay_minutes == pytest.approx(2.5)
        # 1 FP over 25/60 hours = 2.4 FP/hour
        assert res.false_positives_per_hour == pytest.approx(2.4)

    def test_evaluate_predictions_validation_errors(self) -> None:
        with pytest.raises(ValueError, match="Length mismatch"):
            evaluate_predictions(np.array([0, 1]), np.array([0.1]), 0.5)
        with pytest.raises(ValueError, match="empty"):
            evaluate_predictions(np.array([]), np.array([]), 0.5)
        with pytest.raises(ValueError, match="NaN or infinite"):
            evaluate_predictions(np.array([0, 1]), np.array([0.1, np.nan]), 0.5)
        with pytest.raises(ValueError, match="binary"):
            evaluate_predictions(np.array([0, 2]), np.array([0.1, 0.9]), 0.5)
        with pytest.raises(ValueError, match="freq_minutes"):
            evaluate_predictions(np.array([0, 1]), np.array([0.1, 0.9]), 0.5, freq_minutes=0.0)

    def test_calibrate_threshold_separable_and_edge_cases(self) -> None:
        val_labels = np.array([0, 0, 0, 0, 1, 1, 1], dtype=np.int64)
        val_scores = np.array([0.1, 0.2, 0.25, 0.3, 0.8, 0.9, 1.2], dtype=np.float64)

        threshold = calibrate_threshold(val_scores, val_labels)
        assert 0.3 < threshold <= 0.8
        res = evaluate_predictions(val_labels, val_scores, threshold)
        assert res.f1 == pytest.approx(1.0)

        # All-zero labels fallback: above max score
        t_zero = calibrate_threshold(np.array([0.1, 0.4, 0.2]), np.array([0, 0, 0]))
        assert t_zero > 0.4

        # All-one labels fallback: <= min score
        t_one = calibrate_threshold(np.array([0.5, 0.8, 0.9]), np.array([1, 1, 1]))
        assert t_one <= 0.5


class TestRollingZScoreDetector:
    def test_strict_causality_no_future_lookahead(self) -> None:
        rng = np.random.default_rng(7)
        x_train = rng.normal(0.0, 1.0, size=(120, 7))
        x_eval = rng.normal(0.0, 1.0, size=(90, 7))

        detector = RollingZScoreDetector(window_size=20).fit(x_train)
        baseline_scores = detector.score(x_eval)

        assert baseline_scores.shape == (90,)
        assert np.all(np.isfinite(baseline_scores))

        # Mutating future timestep t + 1 must produce ZERO change in scores[0..t]
        for t in (5, 19, 20, 45, 88):
            mutated = x_eval.copy()
            mutated[t + 1] += 1_000.0
            mutated_scores = detector.score(mutated)
            np.testing.assert_array_equal(mutated_scores[: t + 1], baseline_scores[: t + 1])

    def test_short_input_and_error_handling(self) -> None:
        rng = np.random.default_rng(11)
        x_train = rng.normal(0.0, 1.0, size=(60, 7))
        unfitted = RollingZScoreDetector(window_size=30)

        with pytest.raises(RuntimeError):
            unfitted.score(x_train)
        with pytest.raises(RuntimeError):
            unfitted.calibrate_threshold(x_train, np.zeros(60, dtype=np.int64))
        with pytest.raises(RuntimeError):
            unfitted.predict(x_train)

        detector = unfitted.fit(x_train)
        with pytest.raises(RuntimeError, match="threshold is not set"):
            detector.predict(x_train)

        # Input shorter than window_size uses training fallback cleanly with no NaNs
        short = rng.normal(0.0, 1.0, size=(5, 7))
        short_scores = detector.score(short)
        assert short_scores.shape == (5,)
        assert np.all(np.isfinite(short_scores))

        # Invalid matrix shapes/values
        bad = short.copy()
        bad[0, 0] = np.nan
        with pytest.raises(ValueError, match="NaN or infinite"):
            detector.score(bad)
        with pytest.raises(ValueError, match="Expected 7 features"):
            detector.score(np.zeros((5, 3)))

    def test_json_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        rng = np.random.default_rng(19)
        x_train = rng.normal(0.0, 1.0, size=(100, 7))
        x_val = rng.normal(0.0, 1.0, size=(50, 7))
        y_val = np.zeros(50, dtype=np.int64)
        x_val[30:35] += 8.0
        y_val[30:35] = 1

        det = RollingZScoreDetector(window_size=15).fit(x_train)
        det.calibrate_threshold(x_val, y_val)
        expected_scores = det.score(x_val)
        expected_preds = det.predict(x_val)

        out = tmp_path / "rolling_zscore.json"
        det.save(out)
        loaded = RollingZScoreDetector.load(out)

        assert loaded.threshold == pytest.approx(det.threshold)
        np.testing.assert_allclose(loaded.score(x_val), expected_scores, rtol=0.0, atol=1e-12)
        np.testing.assert_array_equal(loaded.predict(x_val), expected_preds)


class TestIsolationForestDetector:
    def test_higher_scores_on_anomalies_and_determinism(self) -> None:
        rng = np.random.default_rng(42)
        x_train = rng.normal(0.0, 1.0, size=(300, 7))
        x_normal = rng.normal(0.0, 1.0, size=(50, 7))
        x_anomaly = rng.normal(6.0, 1.0, size=(20, 7))

        det1 = IsolationForestDetector(n_estimators=100, random_state=42).fit(x_train)
        det2 = IsolationForestDetector(n_estimators=100, random_state=42).fit(x_train)

        s1_norm = det1.score(x_normal)
        s2_norm = det2.score(x_normal)
        s1_anom = det1.score(x_anomaly)

        np.testing.assert_array_equal(s1_norm, s2_norm)
        assert float(np.mean(s1_anom)) > float(np.mean(s1_norm))

    def test_unfitted_errors_and_config_save_load(self, tmp_path: Path) -> None:
        rng = np.random.default_rng(5)
        x_train = rng.normal(0.0, 1.0, size=(120, 7))
        unfitted = IsolationForestDetector(n_estimators=50, random_state=123)

        with pytest.raises(RuntimeError):
            unfitted.score(x_train)
        with pytest.raises(RuntimeError):
            unfitted.predict(x_train)

        det = unfitted.fit(x_train)
        det.set_threshold(0.15)
        with pytest.raises(ValueError, match="NaN or infinite"):
            bad = x_train[:5].copy()
            bad[0, 1] = np.inf
            det.score(bad)

        cfg_path = tmp_path / "iforest_config.json"
        det.save(cfg_path)
        reloaded = IsolationForestDetector.load(cfg_path)
        assert reloaded.n_estimators == 50
        assert reloaded.random_state == 123
        assert reloaded.threshold == pytest.approx(0.15)
        assert reloaded.is_fitted is False

        reloaded.fit(x_train)
        np.testing.assert_array_equal(reloaded.score(x_train[:10]), det.score(x_train[:10]))


def test_end_to_end_leakage_free_benchmark_evaluation() -> None:
    dataset = generate_benchmark_dataset(seed=42, days=7, freq_minutes=1)
    splits = split_chronological(dataset.dataframe, train_ratio=0.6, val_ratio=0.2)

    preprocessor = MultivariatePreprocessor(window_size=30).fit(splits.train)
    x_train = preprocessor.transform(splits.train)
    x_val = preprocessor.transform(splits.val)
    x_test = preprocessor.transform(splits.test)
    y_val = splits.val["is_anomaly"].to_numpy(dtype=np.int64)
    y_test = splits.test["is_anomaly"].to_numpy(dtype=np.int64)

    random_baseline_rate = float(np.mean(y_test))

    detectors = {
        "rolling_zscore": RollingZScoreDetector(window_size=30),
        "isolation_forest": IsolationForestDetector(n_estimators=200, random_state=42),
    }

    for name, detector in detectors.items():
        detector.fit(x_train)
        threshold = detector.calibrate_threshold(x_val, y_val)
        test_scores = detector.score(x_test)
        result = evaluate_predictions(y_test, test_scores, threshold, freq_minutes=1.0)

        assert 0.0 <= result.precision <= 1.0, name
        assert 0.0 <= result.recall <= 1.0, name
        assert 0.0 <= result.f1 <= 1.0, name
        assert result.pr_auc > random_baseline_rate, name
        assert result.total_fault_events == 5, name
        assert result.detected_fault_events > 0, name
        assert result.mean_detection_delay_minutes is not None, name
        assert result.mean_detection_delay_minutes >= 0.0, name
        assert np.isfinite(result.false_positives_per_hour), name
