from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.models import Event, Service
from apps.api.src.schemas.services import ServiceListResponse, ServiceSummary

router = APIRouter(prefix="/api/v1/services", tags=["services"])


@router.get("", response_model=ServiceListResponse, status_code=status.HTTP_200_OK)
def list_services(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ServiceListResponse:
    total = db.scalar(select(func.count(Service.id))) or 0

    stmt = (
        select(
            Service.id,
            Service.name,
            Service.environment,
            Service.owner_team,
            Service.is_auto_discovered,
            func.count(Event.id).label("event_count"),
            func.max(Event.timestamp_utc).label("last_seen_at"),
            Service.created_at,
        )
        .outerjoin(Event, Event.service_id == Service.id)
        .group_by(Service.id)
        .order_by(Service.name.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(stmt).all()
    items = [ServiceSummary.model_validate(row) for row in rows]

    return ServiceListResponse(total=total, limit=limit, offset=offset, items=items)
