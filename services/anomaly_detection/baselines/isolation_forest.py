import json
from pathlib import Path
from typing import Literal

import numpy as np
from sklearn.ensemble import IsolationForest

from services.anomaly_detection.baselines.base import (
    BaseAnomalyDetector,
    validate_feature_matrix,
)


class IsolationForestDetector(BaseAnomalyDetector):
    """Multivariate Isolation Forest baseline detector.

    Wraps `sklearn.ensemble.IsolationForest` fit strictly on normal training data and
    returns `-model.decision_function(X)` so higher scores consistently mean more anomalous.

    Note on serialization: binary `pickle`/`joblib` artifacts are intentionally disallowed
    by project security/portability rules. `save()` / `load()` serialize the deterministic
    hyperparameter configuration and calibrated threshold to JSON; refit on `X_train` with
    the stored `random_state` to reproduce the exact forest.
    """

    def __init__(
        self,
        n_estimators: int = 200,
        contamination: Literal["auto"] | float = "auto",
        max_samples: Literal["auto"] | int | float = "auto",
        random_state: int = 42,
        n_jobs: int = 1,
    ) -> None:
        super().__init__()
        if n_estimators < 1:
            raise ValueError("n_estimators must be at least 1")
        self.n_estimators = n_estimators
        self.contamination = contamination
        self.max_samples = max_samples
        self.random_state = random_state
        self.n_jobs = n_jobs
        self._model: IsolationForest | None = None

    def fit(self, x_train: np.ndarray) -> "IsolationForestDetector":
        arr = validate_feature_matrix(x_train)
        model = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            max_samples=self.max_samples,
            random_state=self.random_state,
            n_jobs=self.n_jobs,
        )
        model.fit(arr)
        self._model = model
        self._n_features = arr.shape[1]
        self._is_fitted = True
        return self

    def score(self, x: np.ndarray) -> np.ndarray:
        if not self._is_fitted or self._model is None:
            raise RuntimeError("IsolationForestDetector must be fitted before calling score()")
        arr = validate_feature_matrix(x, expected_features=self._n_features)
        return np.asarray(-self._model.decision_function(arr), dtype=np.float64)

    def save(self, path: Path) -> None:
        """Save deterministic hyperparameters and threshold to JSON (no pickle/joblib)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "detector": "isolation_forest",
            "format_version": 1,
            "fitted_weights_saved": False,
            "n_estimators": self.n_estimators,
            "contamination": self.contamination,
            "max_samples": self.max_samples,
            "random_state": self.random_state,
            "n_jobs": self.n_jobs,
            "n_features": self._n_features,
            "threshold": self._threshold,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "IsolationForestDetector":
        """Load configuration and threshold from JSON (call fit(X_train) before scoring)."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("detector") != "isolation_forest":
            raise ValueError(f"Unexpected detector type in {path}: {payload.get('detector')!r}")
        detector = cls(
            n_estimators=int(payload["n_estimators"]),
            contamination=payload["contamination"],
            max_samples=payload["max_samples"],
            random_state=int(payload["random_state"]),
            n_jobs=int(payload.get("n_jobs", 1)),
        )
        if payload.get("threshold") is not None:
            detector.set_threshold(float(payload["threshold"]))
        return detector
