import math

import numpy as np
from pydantic import BaseModel, ConfigDict
from sklearn.metrics import average_precision_score, precision_recall_curve


class AnomalyEvaluationResult(BaseModel):
    """Point-wise and operational time-series anomaly detection evaluation metrics."""

    model_config = ConfigDict(frozen=True)

    precision: float
    recall: float
    f1: float
    pr_auc: float
    threshold: float
    total_fault_events: int
    detected_fault_events: int
    mean_detection_delay_minutes: float | None
    per_fault_delays_minutes: list[float | None]
    false_positives_per_hour: float
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int


def _validate_labels_and_scores(
    y_true: np.ndarray,
    scores: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    labels_arr = np.asarray(y_true)
    scores_arr = np.asarray(scores, dtype=np.float64)

    if labels_arr.ndim != 1 or scores_arr.ndim != 1:
        raise ValueError(
            f"y_true and scores must be 1D, got shapes {labels_arr.shape} and {scores_arr.shape}"
        )
    if len(labels_arr) == 0:
        raise ValueError("y_true and scores cannot be empty")
    if len(labels_arr) != len(scores_arr):
        raise ValueError(
            f"Length mismatch: len(y_true)={len(labels_arr)} != len(scores)={len(scores_arr)}"
        )
    if not np.all(np.isfinite(scores_arr)):
        raise ValueError("scores cannot contain NaN or infinite values")

    if labels_arr.dtype == np.bool_:
        labels_int = labels_arr.astype(np.int64)
    else:
        if not np.all(np.isin(labels_arr, [0, 1])):
            raise ValueError("y_true must contain only binary values (0 or 1)")
        labels_int = labels_arr.astype(np.int64)

    return labels_int, scores_arr


def find_fault_intervals(y_true: np.ndarray) -> list[tuple[int, int]]:
    """Return inclusive (start_idx, end_idx) tuples for each contiguous run of 1s in y_true."""
    labels = np.asarray(y_true, dtype=np.int64)
    if labels.ndim != 1:
        raise ValueError("y_true must be a 1D array")

    intervals: list[tuple[int, int]] = []
    in_run = False
    start = 0
    for idx, val in enumerate(labels):
        if val == 1 and not in_run:
            start = idx
            in_run = True
        elif val == 0 and in_run:
            intervals.append((start, idx - 1))
            in_run = False
    if in_run:
        intervals.append((start, len(labels) - 1))
    return intervals


def calibrate_threshold(val_scores: np.ndarray, val_labels: np.ndarray) -> float:
    """Select the decision threshold maximizing F1 strictly on validation scores and labels.

    Uses `sklearn.metrics.precision_recall_curve` where predictions are `score >= threshold`.
    Deterministic tie-breaking prefers the highest threshold achieving the maximum F1 (more
    conservative against false positives) and centers the threshold in the gap below it so
    validation predictions remain identical while improving margin on unseen data.
    """
    y_val, s_val = _validate_labels_and_scores(val_labels, val_scores)

    positives = int(np.sum(y_val == 1))
    if positives == 0:
        return float(np.max(s_val) + 1e-6)
    if positives == len(y_val):
        return float(np.min(s_val))

    precisions, recalls, thresholds = precision_recall_curve(y_val, s_val)
    p = precisions[:-1]
    r = recalls[:-1]
    denom = p + r
    f1s = np.where(denom > 0.0, 2.0 * p * r / denom, 0.0)

    max_f1 = float(np.max(f1s))
    best_indices = np.flatnonzero(np.isclose(f1s, max_f1, rtol=0.0, atol=1e-12))
    # Highest index corresponds to highest threshold (precision_recall_curve thresholds ascend).
    chosen_threshold = float(thresholds[int(best_indices[-1])])

    lower_scores = s_val[s_val < chosen_threshold]
    if len(lower_scores) > 0:
        return float(0.5 * (float(np.max(lower_scores)) + chosen_threshold))
    return chosen_threshold


def evaluate_predictions(
    y_true: np.ndarray,
    scores: np.ndarray,
    threshold: float,
    *,
    freq_minutes: float = 1.0,
) -> AnomalyEvaluationResult:
    """Evaluate continuous anomaly scores and thresholded predictions on a time-series split.

    Operational metric definitions:
    - Contiguous fault event: each maximal contiguous run of `1`s in `y_true`.
    - Detection delay: `(first_pred_positive_idx - fault_start_idx) * freq_minutes` for each
      detected fault event (`None` if no positive prediction falls inside the fault window).
    - `false_positives_per_hour`: `false_positives / normal_hours`, where
      `normal_hours = (count(y_true == 0) * freq_minutes) / 60.0` (`0.0` if `normal_hours == 0`).
    """
    if not math.isfinite(threshold):
        raise ValueError("threshold must be a finite number")
    if not math.isfinite(freq_minutes) or freq_minutes <= 0.0:
        raise ValueError("freq_minutes must be a finite positive number")

    y, s = _validate_labels_and_scores(y_true, scores)
    y_pred = (s >= threshold).astype(np.int64)

    tp = int(np.sum((y == 1) & (y_pred == 1)))
    fp = int(np.sum((y == 0) & (y_pred == 1)))
    tn = int(np.sum((y == 0) & (y_pred == 0)))
    fn = int(np.sum((y == 1) & (y_pred == 0)))

    precision = float(tp / (tp + fp)) if (tp + fp) > 0 else 0.0
    recall = float(tp / (tp + fn)) if (tp + fn) > 0 else 0.0
    f1 = (
        float(2.0 * precision * recall / (precision + recall))
        if (precision + recall) > 0.0
        else 0.0
    )
    pr_auc = float(average_precision_score(y, s)) if np.any(y == 1) else 0.0

    fault_runs = find_fault_intervals(y)
    per_fault_delays: list[float | None] = []
    for start, end in fault_runs:
        window_preds = y_pred[start : end + 1]
        hits = np.flatnonzero(window_preds == 1)
        if len(hits) > 0:
            per_fault_delays.append(float(int(hits[0]) * freq_minutes))
        else:
            per_fault_delays.append(None)

    detected_delays = [d for d in per_fault_delays if d is not None]
    mean_delay = float(np.mean(detected_delays)) if detected_delays else None

    normal_points = int(np.sum(y == 0))
    normal_hours = (normal_points * freq_minutes) / 60.0
    fp_per_hour = float(fp / normal_hours) if normal_hours > 0.0 else 0.0

    return AnomalyEvaluationResult(
        precision=precision,
        recall=recall,
        f1=f1,
        pr_auc=pr_auc,
        threshold=float(threshold),
        total_fault_events=len(fault_runs),
        detected_fault_events=len(detected_delays),
        mean_detection_delay_minutes=mean_delay,
        per_fault_delays_minutes=per_fault_delays,
        false_positives_per_hour=fp_per_hour,
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
    )
