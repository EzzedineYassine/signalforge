import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from sklearn.preprocessing import StandardScaler

from services.anomaly_detection.synthetic_scenarios import FEATURE_COLUMNS


@dataclass(frozen=True)
class ChronologicalSplit:
    """Strictly ordered, temporally disjoint train / validation / test DataFrames."""

    train: pd.DataFrame
    val: pd.DataFrame
    test: pd.DataFrame


def split_chronological(
    df: pd.DataFrame,
    train_ratio: float = 0.6,
    val_ratio: float = 0.2,
    *,
    timestamp_col: str = "timestamp_utc",
    feature_columns: Sequence[str] = FEATURE_COLUMNS,
) -> ChronologicalSplit:
    """Split a UTC time-series DataFrame chronologically without shuffling or boundary overlap."""
    if train_ratio <= 0.0 or val_ratio <= 0.0 or (train_ratio + val_ratio) >= 1.0:
        raise ValueError(
            "Invalid split ratios: train_ratio > 0, val_ratio > 0, and train_ratio + val_ratio < 1"
        )

    required = [timestamp_col, *feature_columns]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}")

    if len(df) < 3:
        raise ValueError("DataFrame must contain at least 3 rows for train/val/test splitting")

    ts = df[timestamp_col]
    if not isinstance(ts.dtype, pd.DatetimeTZDtype):
        raise ValueError(f"{timestamp_col} must be timezone-aware UTC datetime")
    if str(ts.dtype.tz) != "UTC":
        raise ValueError(f"{timestamp_col} timezone must be UTC, got {ts.dtype.tz}")
    if ts.isna().any():
        raise ValueError(f"{timestamp_col} cannot contain null timestamps")
    if not ts.is_monotonic_increasing:
        raise ValueError(f"{timestamp_col} must be sorted in ascending chronological order")

    n = len(df)
    train_end = int(n * train_ratio)
    val_end = int(n * (train_ratio + val_ratio))
    if train_end < 1 or val_end <= train_end or val_end >= n:
        raise ValueError("Split ratios leave at least one empty partition")

    train_df = df.iloc[:train_end].reset_index(drop=True)
    val_df = df.iloc[train_end:val_end].reset_index(drop=True)
    test_df = df.iloc[val_end:].reset_index(drop=True)

    train_max = train_df[timestamp_col].iloc[-1]
    val_min = val_df[timestamp_col].iloc[0]
    val_max = val_df[timestamp_col].iloc[-1]
    test_min = test_df[timestamp_col].iloc[0]
    if not (train_max < val_min and val_max < test_min):
        raise ValueError(
            "Splits are not strictly disjoint in time; duplicate boundary timestamps detected"
        )

    return ChronologicalSplit(train=train_df, val=val_df, test=test_df)


def build_windows(
    values: np.ndarray,
    labels: np.ndarray | None = None,
    window_size: int = 30,
    stride: int = 1,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Build causal sliding windows [t - window_size + 1 .. t] with end-aligned labels[t]."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"values must be a 2D array (n_samples, n_features), got ndim={arr.ndim}")
    if window_size < 1:
        raise ValueError("window_size must be at least 1")
    if stride < 1:
        raise ValueError("stride must be at least 1")
    if len(arr) < window_size:
        raise ValueError(
            f"Input length ({len(arr)}) is smaller than window_size ({window_size})"
        )
    if not np.all(np.isfinite(arr)):
        raise ValueError("values cannot contain NaN or infinite entries")

    windows = sliding_window_view(arr, window_shape=window_size, axis=0)[::stride]
    # sliding_window_view over axis=0 produces (num_windows, n_features, window_size)
    x_windows = np.ascontiguousarray(np.swapaxes(windows, 1, 2))

    if labels is None:
        return x_windows, None

    label_arr = np.asarray(labels)
    if label_arr.ndim != 1 or len(label_arr) != len(arr):
        raise ValueError(
            f"labels must be 1D with length {len(arr)}, got shape {label_arr.shape}"
        )

    end_indices = np.arange(window_size - 1, len(arr), stride)
    y_windows = np.ascontiguousarray(label_arr[end_indices])
    return x_windows, y_windows


class MultivariatePreprocessor:
    """Leakage-free StandardScaler + causal windowing pipeline with JSON state serialization."""

    def __init__(
        self,
        feature_columns: Sequence[str] = FEATURE_COLUMNS,
        window_size: int = 30,
    ) -> None:
        if not feature_columns:
            raise ValueError("feature_columns cannot be empty")
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        self.feature_columns: list[str] = list(feature_columns)
        self.window_size: int = window_size
        self._scaler: StandardScaler | None = None

    @property
    def is_fitted(self) -> bool:
        return self._scaler is not None

    @property
    def mean_(self) -> np.ndarray:
        if self._scaler is None:
            raise RuntimeError("MultivariatePreprocessor must be fitted before accessing mean_")
        return self._scaler.mean_.copy()

    @property
    def scale_(self) -> np.ndarray:
        if self._scaler is None:
            raise RuntimeError("MultivariatePreprocessor must be fitted before accessing scale_")
        return self._scaler.scale_.copy()

    def _extract_matrix(self, df: pd.DataFrame) -> np.ndarray:
        missing = [col for col in self.feature_columns if col not in df.columns]
        if missing:
            raise ValueError(f"Missing required feature column(s): {', '.join(missing)}")
        if len(df) == 0:
            raise ValueError("Input DataFrame cannot be empty")
        matrix = df[self.feature_columns].to_numpy(dtype=np.float64)
        if not np.all(np.isfinite(matrix)):
            raise ValueError("Feature matrix contains NaN or infinite values")
        return matrix

    def fit(self, train_df: pd.DataFrame) -> "MultivariatePreprocessor":
        """Fit StandardScaler strictly on training data only."""
        matrix = self._extract_matrix(train_df)
        scaler = StandardScaler()
        scaler.fit(matrix)
        self._scaler = scaler
        return self

    def transform(self, df: pd.DataFrame) -> np.ndarray:
        """Transform a split DataFrame using training-only scaler parameters."""
        if self._scaler is None:
            raise RuntimeError("MultivariatePreprocessor must be fitted before calling transform()")
        matrix = self._extract_matrix(df)
        return np.asarray(self._scaler.transform(matrix), dtype=np.float64)

    def fit_transform(self, train_df: pd.DataFrame) -> np.ndarray:
        """Fit on train_df and return the scaled training matrix."""
        return self.fit(train_df).transform(train_df)

    def build_windows(
        self,
        values: np.ndarray,
        labels: np.ndarray | None = None,
        window_size: int | None = None,
        stride: int = 1,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        """Build causal windows using this preprocessor's configured window_size by default."""
        size = self.window_size if window_size is None else window_size
        return build_windows(values, labels=labels, window_size=size, stride=stride)

    def save(self, path: Path) -> None:
        """Serialize fitted preprocessor state to JSON (no pickle)."""
        if self._scaler is None:
            raise RuntimeError("MultivariatePreprocessor must be fitted before calling save()")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": 1,
            "feature_columns": self.feature_columns,
            "window_size": self.window_size,
            "mean": [float(v) for v in self._scaler.mean_],
            "scale": [float(v) for v in self._scaler.scale_],
            "var": [float(v) for v in self._scaler.var_],
            "n_samples_seen": int(self._scaler.n_samples_seen_),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "MultivariatePreprocessor":
        """Reconstruct a fitted MultivariatePreprocessor from its JSON state file."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        feature_columns = payload["feature_columns"]
        window_size = int(payload["window_size"])
        mean = np.asarray(payload["mean"], dtype=np.float64)
        scale = np.asarray(payload["scale"], dtype=np.float64)
        var = np.asarray(payload.get("var", scale**2), dtype=np.float64)
        if not (len(feature_columns) == len(mean) == len(scale) == len(var)):
            raise ValueError("Corrupted preprocessor state: feature/parameter length mismatch")

        instance = cls(feature_columns=feature_columns, window_size=window_size)
        scaler = StandardScaler()
        scaler.mean_ = mean
        scaler.scale_ = scale
        scaler.var_ = var
        scaler.n_features_in_ = len(feature_columns)
        scaler.n_samples_seen_ = int(payload.get("n_samples_seen", 0))
        instance._scaler = scaler
        return instance
