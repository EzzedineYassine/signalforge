import math
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from services.anomaly_detection.evaluation.metrics import (
    calibrate_threshold as select_val_threshold,
)


def validate_feature_matrix(
    x: np.ndarray,
    *,
    expected_features: int | None = None,
) -> np.ndarray:
    """Validate that an input feature matrix is 2D, non-empty, finite, and shape-consistent."""
    arr = np.asarray(x, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"Expected a 2D array (n_samples, n_features), got ndim={arr.ndim}")
    if arr.shape[0] == 0 or arr.shape[1] == 0:
        raise ValueError(f"Feature matrix cannot be empty, got shape={arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("Feature matrix cannot contain NaN or infinite values")
    if expected_features is not None and arr.shape[1] != expected_features:
        raise ValueError(
            f"Expected {expected_features} features, got {arr.shape[1]}"
        )
    return arr


class BaseAnomalyDetector(ABC):
    """Unified interface for multivariate time-series anomaly detectors.

    Convention: `score(X)` returns a 1D float64 array where HIGHER scores indicate
    more anomalous observations.
    """

    def __init__(self) -> None:
        self._is_fitted: bool = False
        self._n_features: int | None = None
        self._threshold: float | None = None

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    @property
    def n_features(self) -> int | None:
        return self._n_features

    @property
    def threshold(self) -> float | None:
        return self._threshold

    def set_threshold(self, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("Threshold must be a finite number")
        self._threshold = float(value)

    @abstractmethod
    def fit(self, x_train: np.ndarray) -> "BaseAnomalyDetector":
        """Fit detector parameters strictly on normal training data."""

    @abstractmethod
    def score(self, x: np.ndarray) -> np.ndarray:
        """Return continuous anomaly scores (higher = more anomalous) for each row of x."""

    def calibrate_threshold(self, x_val: np.ndarray, y_val: np.ndarray) -> float:
        """Calibrate decision threshold strictly on validation scores and ground-truth labels."""
        if not self._is_fitted:
            raise RuntimeError(
                f"{type(self).__name__} must be fitted before calling calibrate_threshold()"
            )
        val_scores = self.score(x_val)
        best = select_val_threshold(val_scores, y_val)
        self._threshold = best
        return best

    def predict(self, x: np.ndarray, threshold: float | None = None) -> np.ndarray:
        """Return binary anomaly predictions (0/1) using calibrated or explicit threshold."""
        if not self._is_fitted:
            raise RuntimeError(f"{type(self).__name__} must be fitted before calling predict()")
        active_threshold = self._threshold if threshold is None else threshold
        if active_threshold is None:
            raise RuntimeError(
                f"{type(self).__name__} threshold is not set; "
                "call calibrate_threshold() or pass threshold"
            )
        if not math.isfinite(active_threshold):
            raise ValueError("Threshold must be a finite number")
        scores = self.score(x)
        return (scores >= active_threshold).astype(np.int64)

    @abstractmethod
    def save(self, path: Path) -> None:
        """Persist detector state or configuration to JSON."""

    @classmethod
    @abstractmethod
    def load(cls, path: Path) -> "BaseAnomalyDetector":
        """Load detector state or configuration from JSON."""
