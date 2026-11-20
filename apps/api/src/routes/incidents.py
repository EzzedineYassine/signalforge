from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.models import Incident, Service
from apps.api.src.routes import (
    parse_service_filter,
    parse_severity_filter,
    parse_time_range,
)
from apps.api.src.schemas.incidents import IncidentListResponse, IncidentRead

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


@router.get("", response_model=IncidentListResponse, status_code=http_status.HTTP_200_OK)
def list_incidents(
    db: Annotated[Session, Depends(get_db)],
    service: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IncidentListResponse:
    service_name = parse_service_filter(service)
    normalized_severity = parse_severity_filter(severity)
    start_utc, end_utc = parse_time_range(start_time, end_time)

    cleaned_status: str | None = None
    if status is not None:
        cleaned_status = status.strip().lower()
        if not cleaned_status:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid status filter: status cannot be empty",
            )

    filters = []
    if service_name is not None:
        filters.append(Service.name == service_name)
    if cleaned_status is not None:
        filters.append(Incident.status == cleaned_status)
    if normalized_severity is not None:
        filters.append(Incident.severity == normalized_severity)
    if start_utc is not None:
        filters.append(Incident.started_at >= start_utc)
    if end_utc is not None:
        filters.append(Incident.started_at <= end_utc)

    count_stmt = (
        select(func.count(Incident.id))
        .select_from(Incident)
        .outerjoin(Service, Incident.service_id == Service.id)
        .where(*filters)
    )
    total = db.scalar(count_stmt) or 0

    items_stmt = (
        select(Incident, Service.name.label("service_name"))
        .outerjoin(Service, Incident.service_id == Service.id)
        .where(*filters)
        .order_by(Incident.started_at.desc(), Incident.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(items_stmt).all()

    items = [
        IncidentRead(
            id=incident.id,
            external_id=incident.external_id,
            title=incident.title,
            description=incident.description,
            service_id=incident.service_id,
            service_name=svc_name,
            severity=incident.severity,
            status=incident.status,
            started_at=incident.started_at,
            resolved_at=incident.resolved_at,
            created_at=incident.created_at,
        )
        for incident, svc_name in rows
    ]

    return IncidentListResponse(total=total, limit=limit, offset=offset, items=items)
