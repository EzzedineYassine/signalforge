from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.models import Anomaly, Service
from apps.api.src.routes import parse_service_filter, parse_time_range
from apps.api.src.schemas.anomalies import (
    AnomalyDetectRequest,
    AnomalyDetectResponse,
    AnomalyListResponse,
    AnomalyRead,
)
from apps.api.src.services.anomaly_service import (
    VALID_DETECTORS,
    InsufficientMetricsError,
    ServiceNotFoundError,
    detect_and_persist_anomalies,
)

router = APIRouter(prefix="/api/v1/anomalies", tags=["anomalies"])


def _to_anomaly_read(row: Anomaly, service_name: str) -> AnomalyRead:
    return AnomalyRead(
        id=row.id,
        service_id=row.service_id,
        service_name=service_name,
        detector_name=row.detector_name,
        timestamp_utc=row.timestamp_utc,
        score=row.score,
        threshold=row.threshold,
        is_anomaly=row.is_anomaly,
        metric_names=row.metric_names,
        details=row.details,
        dedup_hash=row.dedup_hash,
        detected_at=row.detected_at,
    )


@router.post("/detect", response_model=AnomalyDetectResponse, status_code=status.HTTP_200_OK)
def detect_anomalies(
    payload: AnomalyDetectRequest,
    db: Annotated[Session, Depends(get_db)],
) -> AnomalyDetectResponse:
    try:
        result = detect_and_persist_anomalies(
            db,
            service_name=payload.service,
            start_time=payload.start_time,
            end_time=payload.end_time,
            detector_name=payload.detector,
            persist_all=payload.persist_all,
        )
    except ServiceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InsufficientMetricsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while running anomaly detection",
        ) from exc

    return AnomalyDetectResponse(
        service=result.service,
        detector=result.detector,
        points_evaluated=result.points_evaluated,
        anomalies_detected=result.anomalies_detected,
        persisted=result.persisted,
        duplicates=result.duplicates,
        threshold=result.threshold,
        max_score=result.max_score,
        items=[_to_anomaly_read(item, result.service) for item in result.items],
    )


@router.get("", response_model=AnomalyListResponse, status_code=status.HTTP_200_OK)
def list_anomalies(
    db: Annotated[Session, Depends(get_db)],
    service: str | None = None,
    detector: str | None = None,
    is_anomaly: bool | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AnomalyListResponse:
    service_name = parse_service_filter(service)
    start_utc, end_utc = parse_time_range(start_time, end_time)

    cleaned_detector: str | None = None
    if detector is not None:
        cleaned_detector = detector.strip().lower()
        if cleaned_detector not in VALID_DETECTORS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid detector filter {detector!r}; expected one of {VALID_DETECTORS}",
            )

    filters = []
    if service_name is not None:
        filters.append(Service.name == service_name)
    if cleaned_detector is not None:
        filters.append(Anomaly.detector_name == cleaned_detector)
    if is_anomaly is not None:
        filters.append(Anomaly.is_anomaly == is_anomaly)
    if start_utc is not None:
        filters.append(Anomaly.timestamp_utc >= start_utc)
    if end_utc is not None:
        filters.append(Anomaly.timestamp_utc <= end_utc)

    count_stmt = (
        select(func.count(Anomaly.id))
        .select_from(Anomaly)
        .join(Service, Anomaly.service_id == Service.id)
        .where(*filters)
    )
    total = db.scalar(count_stmt) or 0

    items_stmt = (
        select(Anomaly, Service.name.label("service_name"))
        .join(Service, Anomaly.service_id == Service.id)
        .where(*filters)
        .order_by(Anomaly.timestamp_utc.desc(), Anomaly.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(items_stmt).all()
    items = [_to_anomaly_read(row, svc_name) for row, svc_name in rows]

    return AnomalyListResponse(total=total, limit=limit, offset=offset, items=items)
