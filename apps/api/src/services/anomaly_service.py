import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import uuid4

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.models import Anomaly, MetricPoint, Service
from services.anomaly_detection import (
    FEATURE_COLUMNS,
    BaseAnomalyDetector,
    IsolationForestDetector,
    LSTMAutoencoderDetector,
    MultivariatePreprocessor,
    RollingZScoreDetector,
    generate_benchmark_dataset,
    split_chronological,
)
from services.ingestion.normalizer import normalize_service_name, normalize_timestamp_utc

logger = logging.getLogger(__name__)

DetectorName = Literal["lstm_autoencoder", "rolling_zscore", "isolation_forest"]
VALID_DETECTORS: tuple[str, ...] = ("lstm_autoencoder", "rolling_zscore", "isolation_forest")

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ARTIFACT_DIR = REPO_ROOT / "experiments" / "benchmarks" / "anomaly_seed42" / "artifacts"


class ServiceNotFoundError(LookupError):
    """Raised when anomaly detection is requested for a service not registered in PostgreSQL."""


class InsufficientMetricsError(ValueError):
    """Raised when no complete 7-feature aligned timestamps exist in the requested window."""


@dataclass(frozen=True)
class AnomalyDetectionResult:
    service: str
    detector: DetectorName
    points_evaluated: int
    anomalies_detected: int
    persisted: int
    duplicates: int
    threshold: float
    max_score: float
    items: list[Anomaly]


def compute_anomaly_dedup_hash(
    service_name: str,
    detector_name: str,
    timestamp_utc: datetime,
) -> str:
    """Compute deterministic 64-char SHA-256 hash for an anomaly detection record."""
    utc_ts = normalize_timestamp_utc(timestamp_utc)
    canonical = f"anomaly|{service_name}|{detector_name}|{utc_ts.isoformat()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@lru_cache(maxsize=8)
def _load_preprocessor_cached(artifact_dir_str: str) -> MultivariatePreprocessor:
    return MultivariatePreprocessor.load(Path(artifact_dir_str) / "preprocessor.json")


@lru_cache(maxsize=16)
def _load_detector_cached(
    detector_name: DetectorName,
    artifact_dir_str: str,
) -> BaseAnomalyDetector:
    artifact_dir = Path(artifact_dir_str)
    if detector_name == "lstm_autoencoder":
        return LSTMAutoencoderDetector.load(artifact_dir / "lstm_autoencoder")
    if detector_name == "rolling_zscore":
        return RollingZScoreDetector.load(artifact_dir / "rolling_zscore.json")
    if detector_name == "isolation_forest":
        detector = IsolationForestDetector.load(artifact_dir / "isolation_forest_config.json")
        saved_threshold = detector.threshold
        preprocessor = _load_preprocessor_cached(artifact_dir_str)
        dataset = generate_benchmark_dataset(seed=detector.random_state, days=7, freq_minutes=1)
        splits = split_chronological(dataset.dataframe, train_ratio=0.6, val_ratio=0.2)
        x_train = preprocessor.transform(splits.train)
        detector.fit(x_train)
        if saved_threshold is not None:
            detector.set_threshold(saved_threshold)
        return detector
    raise ValueError(f"Unsupported detector: {detector_name!r}")


def _pivot_aligned_metrics(
    rows: list[tuple[datetime, str, float]],
) -> tuple[list[datetime], np.ndarray]:
    """Pivot metric_points rows into strictly aligned chronological (N, 7) float64 arrays."""
    buckets: dict[datetime, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for ts, metric_name, value in rows:
        utc_ts = ts.astimezone(UTC)
        buckets[utc_ts][metric_name].append(float(value))

    aligned_timestamps: list[datetime] = []
    aligned_rows: list[list[float]] = []
    required_set = set(FEATURE_COLUMNS)

    for utc_ts in sorted(buckets):
        metric_map = buckets[utc_ts]
        if not required_set.issubset(metric_map.keys()):
            continue
        aligned_timestamps.append(utc_ts)
        aligned_rows.append(
            [float(np.mean(metric_map[col])) for col in FEATURE_COLUMNS]
        )

    if not aligned_timestamps:
        return [], np.empty((0, len(FEATURE_COLUMNS)), dtype=np.float64)

    return aligned_timestamps, np.asarray(aligned_rows, dtype=np.float64)


def detect_and_persist_anomalies(
    db: Session,
    *,
    service_name: str,
    start_time: datetime,
    end_time: datetime,
    detector_name: DetectorName = "lstm_autoencoder",
    persist_all: bool = False,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
) -> AnomalyDetectionResult:
    """Query aligned service metrics, run anomaly detection, and persist results idempotently."""
    normalized_service = normalize_service_name(service_name)
    start_utc = normalize_timestamp_utc(start_time)
    end_utc = normalize_timestamp_utc(end_time)
    if start_utc > end_utc:
        raise ValueError("start_time must be less than or equal to end_time")

    service = db.scalar(select(Service).where(Service.name == normalized_service))
    if service is None:
        raise ServiceNotFoundError(f"Service not found: {normalized_service!r}")

    metric_rows = db.execute(
        select(MetricPoint.timestamp_utc, MetricPoint.metric_name, MetricPoint.value)
        .where(
            MetricPoint.service_id == service.id,
            MetricPoint.metric_name.in_(FEATURE_COLUMNS),
            MetricPoint.timestamp_utc >= start_utc,
            MetricPoint.timestamp_utc <= end_utc,
        )
        .order_by(MetricPoint.timestamp_utc.asc(), MetricPoint.id.asc())
    ).all()

    timestamps, raw_matrix = _pivot_aligned_metrics(
        [(ts, name, val) for ts, name, val in metric_rows]
    )
    if not timestamps:
        raise InsufficientMetricsError(
            f"No aligned 7-feature metric points found for service {service.name!r} "
            f"between {start_utc.isoformat()} and {end_utc.isoformat()}"
        )

    artifact_dir_str = str(artifact_dir.resolve())
    preprocessor = _load_preprocessor_cached(artifact_dir_str)
    detector = _load_detector_cached(detector_name, artifact_dir_str)
    if detector.threshold is None:
        raise RuntimeError(f"Loaded detector {detector_name!r} lacks a calibrated threshold")

    df = pd.DataFrame(raw_matrix, columns=list(FEATURE_COLUMNS))
    scaled = preprocessor.transform(df)
    scores = detector.score(scaled)
    threshold = float(detector.threshold)
    flags = scores >= threshold

    anomalies_detected = int(np.sum(flags))
    max_score = float(np.max(scores))

    rows_to_persist: list[dict[str, object]] = []
    dedup_hashes: list[str] = []

    for idx, utc_ts in enumerate(timestamps):
        is_anom = bool(flags[idx])
        if not persist_all and not is_anom:
            continue

        top_feature = FEATURE_COLUMNS[int(np.argmax(np.abs(scaled[idx])))]
        feature_values = {
            col: float(raw_matrix[idx, col_idx])
            for col_idx, col in enumerate(FEATURE_COLUMNS)
        }
        dedup_hash = compute_anomaly_dedup_hash(service.name, detector_name, utc_ts)
        dedup_hashes.append(dedup_hash)
        rows_to_persist.append(
            {
                "id": uuid4(),
                "service_id": service.id,
                "detector_name": detector_name,
                "timestamp_utc": utc_ts,
                "score": float(scores[idx]),
                "threshold": threshold,
                "is_anomaly": is_anom,
                "metric_names": list(FEATURE_COLUMNS),
                "details": {
                    "top_feature": top_feature,
                    "feature_values": feature_values,
                },
                "dedup_hash": dedup_hash,
            }
        )

    persisted = 0
    duplicates = 0
    returned_items: list[Anomaly] = []

    try:
        if rows_to_persist:
            inserted_ids = (
                db.execute(
                    pg_insert(Anomaly)
                    .values(rows_to_persist)
                    .on_conflict_do_nothing(index_elements=["dedup_hash"])
                    .returning(Anomaly.id)
                )
                .scalars()
                .all()
            )
            persisted = len(inserted_ids)
            duplicates = len(rows_to_persist) - persisted
            db.commit()

            returned_items = list(
                db.scalars(
                    select(Anomaly)
                    .where(Anomaly.dedup_hash.in_(dedup_hashes))
                    .order_by(Anomaly.timestamp_utc.desc(), Anomaly.id.desc())
                ).all()
            )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Anomaly persistence failed: service=%s detector=%s error=%s",
            service.name,
            detector_name,
            type(exc).__name__,
        )
        raise

    logger.info(
        "Anomaly detection completed: service=%s detector=%s points=%d detected=%d "
        "persisted=%d duplicates=%d",
        service.name,
        detector_name,
        len(timestamps),
        anomalies_detected,
        persisted,
        duplicates,
    )

    return AnomalyDetectionResult(
        service=service.name,
        detector=detector_name,
        points_evaluated=len(timestamps),
        anomalies_detected=anomalies_detected,
        persisted=persisted,
        duplicates=duplicates,
        threshold=threshold,
        max_score=max_score,
        items=returned_items,
    )
