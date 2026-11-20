import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.models import Event, IngestionFailure, Service
from services.ingestion.validator import (
    NormalizedEvent,
    ValidationFailure,
    validate_and_normalize_batch,
)

logger = logging.getLogger(__name__)


class IngestionBatchResult(BaseModel):
    """Structured result returned after persisting an event batch."""

    model_config = ConfigDict(frozen=True)

    batch_id: UUID
    received: int
    accepted: int
    duplicates: int
    rejected: int
    errors: list[ValidationFailure] = Field(default_factory=list)


def _deduplicate_in_batch(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    unique_events: list[NormalizedEvent] = []
    seen_hashes: set[str] = set()
    for event in events:
        if event.dedup_hash not in seen_hashes:
            seen_hashes.add(event.dedup_hash)
            unique_events.append(event)
    return unique_events


def ingest_event_batch(db: Session, raw_records: Sequence[Any]) -> IngestionBatchResult:
    """Validate, normalize, and idempotently persist a batch of raw event records."""
    batch_id = uuid4()
    received = len(raw_records)
    validation = validate_and_normalize_batch(raw_records)
    accepted = 0

    try:
        if validation.valid_events:
            service_names = sorted({event.service_name for event in validation.valid_events})
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
                pg_insert(Service)
                .values(service_rows)
                .on_conflict_do_nothing(index_elements=["name"])
            )

            existing_services = db.execute(
                select(Service.name, Service.id).where(Service.name.in_(service_names))
            ).all()
            service_id_by_name = {name: service_id for name, service_id in existing_services}

            unique_events = _deduplicate_in_batch(validation.valid_events)
            event_rows = [
                {
                    "id": uuid4(),
                    "service_id": service_id_by_name[event.service_name],
                    "external_id": event.external_id,
                    "dedup_hash": event.dedup_hash,
                    "timestamp_utc": event.timestamp_utc,
                    "event_type": event.event_type,
                    "severity": event.severity,
                    "message": event.message,
                    "trace_id": event.trace_id,
                    "span_id": event.span_id,
                    "attributes": event.attributes,
                }
                for event in unique_events
            ]
            inserted_ids = (
                db.execute(
                    pg_insert(Event)
                    .values(event_rows)
                    .on_conflict_do_nothing(index_elements=["dedup_hash"])
                    .returning(Event.id)
                )
                .scalars()
                .all()
            )
            accepted = len(inserted_ids)

        if validation.failures:
            failure_rows = [
                {
                    "id": uuid4(),
                    "batch_id": batch_id,
                    "record_index": failure.record_index,
                    "error_code": failure.error_code,
                    "error_detail": failure.error_detail,
                    "raw_payload": failure.raw_payload,
                }
                for failure in validation.failures
            ]
            db.execute(pg_insert(IngestionFailure).values(failure_rows))

        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Event batch persistence failed: batch_id=%s error=%s",
            batch_id,
            type(exc).__name__,
        )
        raise

    duplicates = len(validation.valid_events) - accepted
    rejected = len(validation.failures)

    logger.info(
        "Event batch ingested: batch_id=%s received=%d accepted=%d duplicates=%d rejected=%d",
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
