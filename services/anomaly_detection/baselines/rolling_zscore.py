import json
import math
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from services.anomaly_detection.baselines.base import (
    BaseAnomalyDetector,
    validate_feature_matrix,
)


class RollingZScoreDetector(BaseAnomalyDetector):
    """Strictly causal multivariate rolling Z-score baseline detector.

    For each timestep `t >= window_size - 1`, computes feature-wise mean and std over
    the trailing causal window `[t - window_size + 1 .. t]` (zero future lookahead) and
    aggregates the anomaly score as `max(abs(z_score))` across features. For the initial
    `window_size - 1` steps, falls back to training-set feature means/stds learned in `fit()`.
    """

    def __init__(self, window_size: int = 30, min_std: float = 1e-6) -> None:
        super().__init__()
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        if not math.isfinite(min_std) or min_std <= 0.0:
            raise ValueError("min_std must be a finite positive number")
        self.window_size = window_size
        self.min_std = float(min_std)
        self._train_mean: np.ndarray | None = None
        self._train_std: np.ndarray | None = None

    def fit(self, x_train: np.ndarray) -> "RollingZScoreDetector":
        arr = validate_feature_matrix(x_train)
        self._train_mean = np.mean(arr, axis=0)
        self._train_std = np.maximum(np.std(arr, axis=0), self.min_std)
        self._n_features = arr.shape[1]
        self._is_fitted = True
        return self

    def score(self, x: np.ndarray) -> np.ndarray:
        if not self._is_fitted or self._train_mean is None or self._train_std is None:
            raise RuntimeError("RollingZScoreDetector must be fitted before calling score()")

        arr = validate_feature_matrix(x, expected_features=self._n_features)
        n_samples = arr.shape[0]
        scores = np.empty(n_samples, dtype=np.float64)

        head_len = min(n_samples, self.window_size - 1)
        if head_len > 0:
            z_head = np.abs(arr[:head_len] - self._train_mean) / self._train_std
            scores[:head_len] = np.max(z_head, axis=1)

        if n_samples >= self.window_size:
            # sliding_window_view over axis=0 has shape (n_windows, n_features, window_size)
            windows = sliding_window_view(arr, window_shape=self.window_size, axis=0)
            roll_mean = np.mean(windows, axis=2)
            roll_std = np.maximum(np.std(windows, axis=2), self.min_std)
            tail = arr[self.window_size - 1 :]
            z_tail = np.abs(tail - roll_mean) / roll_std
            scores[self.window_size - 1 :] = np.max(z_tail, axis=1)

        return scores

    def save(self, path: Path) -> None:
        if not self._is_fitted or self._train_mean is None or self._train_std is None:
            raise RuntimeError("RollingZScoreDetector must be fitted before calling save()")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "detector": "rolling_zscore",
            "format_version": 1,
            "window_size": self.window_size,
            "min_std": self.min_std,
            "n_features": self._n_features,
            "train_mean": [float(v) for v in self._train_mean],
            "train_std": [float(v) for v in self._train_std],
            "threshold": self._threshold,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "RollingZScoreDetector":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("detector") != "rolling_zscore":
            raise ValueError(f"Unexpected detector type in {path}: {payload.get('detector')!r}")
        train_mean = np.asarray(payload["train_mean"], dtype=np.float64)
        train_std = np.asarray(payload["train_std"], dtype=np.float64)
        n_features = int(payload["n_features"])
        if not (len(train_mean) == len(train_std) == n_features):
            raise ValueError("Corrupted RollingZScoreDetector state: feature length mismatch")

        detector = cls(
            window_size=int(payload["window_size"]),
            min_std=float(payload["min_std"]),
        )
        detector._train_mean = train_mean
        detector._train_std = train_std
        detector._n_features = n_features
        detector._is_fitted = True
        if payload.get("threshold") is not None:
            detector.set_threshold(float(payload["threshold"]))
        return detector
