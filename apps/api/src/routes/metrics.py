from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.models import MetricPoint, Service
from apps.api.src.routes import (
    parse_metric_name_filter,
    parse_service_filter,
    parse_time_range,
)
from apps.api.src.schemas.events import IngestionErrorItem
from apps.api.src.schemas.metrics import (
    MetricIngestRequest,
    MetricIngestResponse,
    MetricListResponse,
    MetricPointRead,
)
from services.ingestion.pipeline import IngestionBatchResult
from services.ingestion.telemetry_pipeline import ingest_metric_batch, ingest_metrics_csv
from services.ingestion.telemetry_validator import CsvFormatError

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


def _to_ingest_response(result: IngestionBatchResult) -> MetricIngestResponse:
    return MetricIngestResponse(
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


@router.post("/ingest", response_model=MetricIngestResponse, status_code=status.HTTP_200_OK)
def ingest_metrics(
    payload: MetricIngestRequest,
    db: Annotated[Session, Depends(get_db)],
) -> MetricIngestResponse:
    try:
        result = ingest_metric_batch(db, payload.metrics)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while ingesting metric batch",
        ) from exc
    return _to_ingest_response(result)


@router.post("/ingest/csv", response_model=MetricIngestResponse, status_code=status.HTTP_200_OK)
async def ingest_metrics_csv_route(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> MetricIngestResponse:
    raw_body = await request.body()
    try:
        csv_text = raw_body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="CSV request body must be valid UTF-8",
        ) from exc

    try:
        result = ingest_metrics_csv(db, csv_text)
    except CsvFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while ingesting CSV metric batch",
        ) from exc

    return _to_ingest_response(result)


@router.get("", response_model=MetricListResponse, status_code=status.HTTP_200_OK)
def list_metrics(
    db: Annotated[Session, Depends(get_db)],
    service: str | None = None,
    metric_name: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MetricListResponse:
    service_name = parse_service_filter(service)
    normalized_metric = parse_metric_name_filter(metric_name)
    start_utc, end_utc = parse_time_range(start_time, end_time)

    filters = []
    if service_name is not None:
        filters.append(Service.name == service_name)
    if normalized_metric is not None:
        filters.append(MetricPoint.metric_name == normalized_metric)
    if start_utc is not None:
        filters.append(MetricPoint.timestamp_utc >= start_utc)
    if end_utc is not None:
        filters.append(MetricPoint.timestamp_utc <= end_utc)

    count_stmt = (
        select(func.count(MetricPoint.id))
        .select_from(MetricPoint)
        .join(Service, MetricPoint.service_id == Service.id)
        .where(*filters)
    )
    total = db.scalar(count_stmt) or 0

    items_stmt = (
        select(MetricPoint, Service.name.label("service_name"))
        .join(Service, MetricPoint.service_id == Service.id)
        .where(*filters)
        .order_by(MetricPoint.timestamp_utc.asc(), MetricPoint.id.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(items_stmt).all()

    items = [
        MetricPointRead(
            id=point.id,
            service_id=point.service_id,
            service_name=svc_name,
            metric_name=point.metric_name,
            timestamp_utc=point.timestamp_utc,
            value=point.value,
            unit=point.unit,
            labels=point.labels,
            dedup_hash=point.dedup_hash,
            ingested_at=point.ingested_at,
        )
        for point, svc_name in rows
    ]

    return MetricListResponse(total=total, limit=limit, offset=offset, items=items)
