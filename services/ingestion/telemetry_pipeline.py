import logging
from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.models import Deployment, IngestionFailure, MetricPoint, Service, TraceSpan
from services.ingestion.pipeline import IngestionBatchResult
from services.ingestion.telemetry_validator import (
    MetricBatchValidationResult,
    NormalizedMetricPoint,
    NormalizedTraceSpan,
    parse_and_validate_metrics_csv,
    validate_and_normalize_deployment_batch,
    validate_and_normalize_metric_batch,
    validate_and_normalize_trace_batch,
)
from services.ingestion.validator import ValidationFailure

logger = logging.getLogger(__name__)


def _upsert_services(db: Session, names: Iterable[str]) -> dict[str, UUID]:
    service_names = sorted(set(names))
    if not service_names:
        return {}
    service_rows = [
        {
            "id": uuid4(),
            "name": name,
            "environment": "default",
            "is_auto_discovered": True,
        }
        for name in service_names
    ]
    db.execute(
        pg_insert(Service).values(service_rows).on_conflict_do_nothing(index_elements=["name"])
    )
    rows = db.execute(select(Service.name, Service.id).where(Service.name.in_(service_names))).all()
    return {name: service_id for name, service_id in rows}


def _persist_failures(
    db: Session,
    batch_id: UUID,
    failures: Sequence[ValidationFailure],
) -> None:
    if not failures:
        return
    failure_rows = [
        {
            "id": uuid4(),
            "batch_id": batch_id,
            "record_index": failure.record_index,
            "error_code": failure.error_code,
            "error_detail": failure.error_detail,
            "raw_payload": failure.raw_payload,
        }
        for failure in failures
    ]
    db.execute(pg_insert(IngestionFailure).values(failure_rows))


def _deduplicate_metrics_in_batch(
    points: Sequence[NormalizedMetricPoint],
) -> list[NormalizedMetricPoint]:
    unique: list[NormalizedMetricPoint] = []
    seen: set[str] = set()
    for point in points:
        if point.dedup_hash not in seen:
            seen.add(point.dedup_hash)
            unique.append(point)
    return unique


def _deduplicate_spans_in_batch(
    spans: Sequence[NormalizedTraceSpan],
) -> list[NormalizedTraceSpan]:
    unique: list[NormalizedTraceSpan] = []
    seen: set[tuple[str, str]] = set()
    for span in spans:
        key = (span.trace_id, span.span_id)
        if key not in seen:
            seen.add(key)
            unique.append(span)
    return unique


def _persist_metric_validation(
    db: Session,
    *,
    received: int,
    validation: MetricBatchValidationResult,
) -> IngestionBatchResult:
    batch_id = uuid4()
    accepted = 0

    try:
        if validation.valid_points:
            service_ids = _upsert_services(
                db, (point.service_name for point in validation.valid_points)
            )
            unique_points = _deduplicate_metrics_in_batch(validation.valid_points)
            metric_rows = [
                {
                    "id": uuid4(),
                    "service_id": service_ids[point.service_name],
                    "metric_name": point.metric_name,
                    "timestamp_utc": point.timestamp_utc,
                    "value": point.value,
                    "unit": point.unit,
                    "labels": point.labels,
                    "dedup_hash": point.dedup_hash,
                }
                for point in unique_points
            ]
            inserted_ids = (
                db.execute(
                    pg_insert(MetricPoint)
                    .values(metric_rows)
                    .on_conflict_do_nothing(index_elements=["dedup_hash"])
                    .returning(MetricPoint.id)
                )
                .scalars()
                .all()
            )
            accepted = len(inserted_ids)

        _persist_failures(db, batch_id, validation.failures)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Metric batch persistence failed: batch_id=%s error=%s",
            batch_id,
            type(exc).__name__,
        )
        raise

    duplicates = len(validation.valid_points) - accepted
    rejected = len(validation.failures)
    logger.info(
        "Metric batch ingested: batch_id=%s received=%d accepted=%d duplicates=%d rejected=%d",
        batch_id,
        received,
        accepted,
        duplicates,
        rejected,
    )
    return IngestionBatchResult(
        batch_id=batch_id,
        received=received,
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        errors=validation.failures,
    )


def ingest_metric_batch(db: Session, raw_records: Sequence[Any]) -> IngestionBatchResult:
    """Validate, normalize, and idempotently persist a batch of JSON metric points."""
    validation = validate_and_normalize_metric_batch(raw_records)
    return _persist_metric_validation(db, received=len(raw_records), validation=validation)


def ingest_metrics_csv(
    db: Session,
    csv_text: str,
    *,
    max_rows: int = 5000,
) -> IngestionBatchResult:
    """Parse UTF-8 CSV metric rows and persist valid points / failures idempotently."""
    validation = parse_and_validate_metrics_csv(csv_text, max_rows=max_rows)
    received = len(validation.valid_points) + len(validation.failures)
    return _persist_metric_validation(db, received=received, validation=validation)


def ingest_trace_batch(db: Session, raw_records: Sequence[Any]) -> IngestionBatchResult:
    """Validate, normalize, and idempotently persist a batch of distributed trace spans."""
    batch_id = uuid4()
    received = len(raw_records)
    validation = validate_and_normalize_trace_batch(raw_records)
    accepted = 0

    try:
        if validation.valid_spans:
            service_ids = _upsert_services(
                db, (span.service_name for span in validation.valid_spans)
            )
            unique_spans = _deduplicate_spans_in_batch(validation.valid_spans)
            span_rows = [
                {
                    "id": uuid4(),
                    "service_id": service_ids[span.service_name],
                    "trace_id": span.trace_id,
                    "span_id": span.span_id,
                    "parent_span_id": span.parent_span_id,
                    "operation_name": span.operation_name,
                    "span_kind": span.span_kind,
                    "status_code": span.status_code,
                    "start_time_utc": span.start_time_utc,
                    "end_time_utc": span.end_time_utc,
                    "duration_ms": span.duration_ms,
                    "attributes": span.attributes,
                }
                for span in unique_spans
            ]
            inserted_ids = (
                db.execute(
                    pg_insert(TraceSpan)
                    .values(span_rows)
                    .on_conflict_do_nothing(index_elements=["trace_id", "span_id"])
                    .returning(TraceSpan.id)
                )
                .scalars()
                .all()
            )
            accepted = len(inserted_ids)

        _persist_failures(db, batch_id, validation.failures)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Trace batch persistence failed: batch_id=%s error=%s",
            batch_id,
            type(exc).__name__,
        )
        raise

    duplicates = len(validation.valid_spans) - accepted
    rejected = len(validation.failures)
    logger.info(
        "Trace batch ingested: batch_id=%s received=%d accepted=%d duplicates=%d rejected=%d",
        batch_id,
        received,
        accepted,
        duplicates,
        rejected,
    )
    return IngestionBatchResult(
        batch_id=batch_id,
        received=received,
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        errors=validation.failures,
    )


def ingest_deployment_batch(db: Session, raw_records: Sequence[Any]) -> IngestionBatchResult:
    """Validate, normalize, and persist a batch of deployment records."""
    batch_id = uuid4()
    received = len(raw_records)
    validation = validate_and_normalize_deployment_batch(raw_records)
    accepted = 0

    try:
        if validation.valid_deployments:
            service_ids = _upsert_services(
                db, (dep.service_name for dep in validation.valid_deployments)
            )
            deployment_rows = [
                {
                    "id": uuid4(),
                    "service_id": service_ids[dep.service_name],
                    "version": dep.version,
                    "commit_sha": dep.commit_sha,
                    "status": dep.status,
                    "deployed_by": dep.deployed_by,
                    "deployed_at": dep.deployed_at,
                    "metadata_json": dep.metadata_json,
                }
                for dep in validation.valid_deployments
            ]
            inserted_ids = (
                db.execute(
                    pg_insert(Deployment).values(deployment_rows).returning(Deployment.id)
                )
                .scalars()
                .all()
            )
            accepted = len(inserted_ids)

        _persist_failures(db, batch_id, validation.failures)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Deployment batch persistence failed: batch_id=%s error=%s",
            batch_id,
            type(exc).__name__,
        )
        raise

    rejected = len(validation.failures)
    logger.info(
        "Deployment batch ingested: batch_id=%s received=%d accepted=%d duplicates=0 rejected=%d",
        batch_id,
        received,
        accepted,
        rejected,
    )
    return IngestionBatchResult(
        batch_id=batch_id,
        received=received,
        accepted=accepted,
        duplicates=0,
        rejected=rejected,
        errors=validation.failures,
    )
