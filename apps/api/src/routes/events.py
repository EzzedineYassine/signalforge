from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.models import Event, Service
from apps.api.src.routes import (
    parse_event_type_filter,
    parse_service_filter,
    parse_severity_filter,
    parse_time_range,
)
from apps.api.src.schemas.events import (
    EventIngestRequest,
    EventIngestResponse,
    EventListResponse,
    EventRead,
    IngestionErrorItem,
)
from services.ingestion.pipeline import ingest_event_batch

router = APIRouter(prefix="/api/v1/events", tags=["events"])


@router.post("/ingest", response_model=EventIngestResponse, status_code=status.HTTP_200_OK)
def ingest_events(
    payload: EventIngestRequest,
    db: Annotated[Session, Depends(get_db)],
) -> EventIngestResponse:
    try:
        result = ingest_event_batch(db, payload.events)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while ingesting event batch",
        ) from exc

    return EventIngestResponse(
        batch_id=result.batch_id,
        received=result.received,
        accepted=result.accepted,
        duplicates=result.duplicates,
        rejected=result.rejected,
        errors=[
            IngestionErrorItem(
                index=err.record_index,
                error_code=err.error_code,
                error_detail=err.error_detail,
            )
            for err in result.errors
        ],
    )


@router.get("", response_model=EventListResponse, status_code=status.HTTP_200_OK)
def list_events(
    db: Annotated[Session, Depends(get_db)],
    service: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    severity: str | None = None,
    event_type: str | None = None,
    trace_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventListResponse:
    service_name = parse_service_filter(service)
    start_utc, end_utc = parse_time_range(start_time, end_time)
    normalized_severity = parse_severity_filter(severity)
    normalized_event_type = parse_event_type_filter(event_type)

    cleaned_trace_id: str | None = None
    if trace_id is not None:
        cleaned_trace_id = trace_id.strip()
        if not cleaned_trace_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid trace_id filter: trace_id cannot be empty",
            )

    filters = []
    if service_name is not None:
        filters.append(Service.name == service_name)
    if start_utc is not None:
        filters.append(Event.timestamp_utc >= start_utc)
    if end_utc is not None:
        filters.append(Event.timestamp_utc <= end_utc)
    if normalized_severity is not None:
        filters.append(Event.severity == normalized_severity)
    if normalized_event_type is not None:
        filters.append(Event.event_type == normalized_event_type)
    if cleaned_trace_id is not None:
        filters.append(Event.trace_id == cleaned_trace_id)

    count_stmt = (
        select(func.count(Event.id))
        .select_from(Event)
        .join(Service, Event.service_id == Service.id)
        .where(*filters)
    )
    total = db.scalar(count_stmt) or 0

    items_stmt = (
        select(Event, Service.name.label("service_name"))
        .join(Service, Event.service_id == Service.id)
        .where(*filters)
        .order_by(Event.timestamp_utc.desc(), Event.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(items_stmt).all()

    items = [
        EventRead(
            id=event.id,
            service_id=event.service_id,
            service_name=svc_name,
            external_id=event.external_id,
            dedup_hash=event.dedup_hash,
            timestamp_utc=event.timestamp_utc,
            event_type=event.event_type,
            severity=event.severity,
            message=event.message,
            trace_id=event.trace_id,
            span_id=event.span_id,
            attributes=event.attributes,
            ingested_at=event.ingested_at,
        )
        for event, svc_name in rows
    ]

    return EventListResponse(total=total, limit=limit, offset=offset, items=items)
