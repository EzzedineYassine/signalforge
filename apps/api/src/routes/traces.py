from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.models import Service, TraceSpan
from apps.api.src.routes import (
    parse_service_filter,
    parse_span_status_filter,
    parse_time_range,
)
from apps.api.src.schemas.events import IngestionErrorItem
from apps.api.src.schemas.traces import (
    TraceIngestRequest,
    TraceIngestResponse,
    TraceListResponse,
    TraceSpanRead,
)
from services.ingestion.telemetry_pipeline import ingest_trace_batch

router = APIRouter(prefix="/api/v1/traces", tags=["traces"])


@router.post("/ingest", response_model=TraceIngestResponse, status_code=status.HTTP_200_OK)
def ingest_traces(
    payload: TraceIngestRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TraceIngestResponse:
    try:
        result = ingest_trace_batch(db, payload.spans)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while ingesting trace batch",
        ) from exc

    return TraceIngestResponse(
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


@router.get("", response_model=TraceListResponse, status_code=status.HTTP_200_OK)
def list_traces(
    db: Annotated[Session, Depends(get_db)],
    trace_id: str | None = None,
    service: str | None = None,
    status_code: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TraceListResponse:
    service_name = parse_service_filter(service)
    normalized_status = parse_span_status_filter(status_code)
    start_utc, end_utc = parse_time_range(start_time, end_time)

    cleaned_trace_id: str | None = None
    if trace_id is not None:
        cleaned_trace_id = trace_id.strip()
        if not cleaned_trace_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid trace_id filter: trace_id cannot be empty",
            )

    filters = []
    if cleaned_trace_id is not None:
        filters.append(TraceSpan.trace_id == cleaned_trace_id)
    if service_name is not None:
        filters.append(Service.name == service_name)
    if normalized_status is not None:
        filters.append(TraceSpan.status_code == normalized_status)
    if start_utc is not None:
        filters.append(TraceSpan.start_time_utc >= start_utc)
    if end_utc is not None:
        filters.append(TraceSpan.start_time_utc <= end_utc)

    count_stmt = (
        select(func.count(TraceSpan.id))
        .select_from(TraceSpan)
        .join(Service, TraceSpan.service_id == Service.id)
        .where(*filters)
    )
    total = db.scalar(count_stmt) or 0

    items_stmt = (
        select(TraceSpan, Service.name.label("service_name"))
        .join(Service, TraceSpan.service_id == Service.id)
        .where(*filters)
        .order_by(TraceSpan.start_time_utc.desc(), TraceSpan.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(items_stmt).all()

    items = [
        TraceSpanRead(
            id=span.id,
            service_id=span.service_id,
            service_name=svc_name,
            trace_id=span.trace_id,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            operation_name=span.operation_name,
            span_kind=span.span_kind,
            status_code=span.status_code,
            start_time_utc=span.start_time_utc,
            end_time_utc=span.end_time_utc,
            duration_ms=span.duration_ms,
            attributes=span.attributes,
            ingested_at=span.ingested_at,
        )
        for span, svc_name in rows
    ]

    return TraceListResponse(total=total, limit=limit, offset=offset, items=items)
