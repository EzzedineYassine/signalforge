from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.models import Deployment, Service
from apps.api.src.routes import (
    parse_deployment_status_filter,
    parse_service_filter,
    parse_time_range,
)
from apps.api.src.schemas.deployments import (
    DeploymentIngestRequest,
    DeploymentIngestResponse,
    DeploymentListResponse,
    DeploymentRead,
)
from apps.api.src.schemas.events import IngestionErrorItem
from services.ingestion.telemetry_pipeline import ingest_deployment_batch

router = APIRouter(prefix="/api/v1/deployments", tags=["deployments"])


@router.post(
    "/ingest",
    response_model=DeploymentIngestResponse,
    status_code=http_status.HTTP_200_OK,
)
def ingest_deployments(
    payload: DeploymentIngestRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DeploymentIngestResponse:
    try:
        result = ingest_deployment_batch(db, payload.deployments)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while ingesting deployment batch",
        ) from exc

    return DeploymentIngestResponse(
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


@router.get("", response_model=DeploymentListResponse, status_code=http_status.HTTP_200_OK)
def list_deployments(
    db: Annotated[Session, Depends(get_db)],
    service: str | None = None,
    status: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DeploymentListResponse:
    service_name = parse_service_filter(service)
    normalized_status = parse_deployment_status_filter(status)
    start_utc, end_utc = parse_time_range(start_time, end_time)

    filters = []
    if service_name is not None:
        filters.append(Service.name == service_name)
    if normalized_status is not None:
        filters.append(Deployment.status == normalized_status)
    if start_utc is not None:
        filters.append(Deployment.deployed_at >= start_utc)
    if end_utc is not None:
        filters.append(Deployment.deployed_at <= end_utc)

    count_stmt = (
        select(func.count(Deployment.id))
        .select_from(Deployment)
        .join(Service, Deployment.service_id == Service.id)
        .where(*filters)
    )
    total = db.scalar(count_stmt) or 0

    items_stmt = (
        select(Deployment, Service.name.label("service_name"))
        .join(Service, Deployment.service_id == Service.id)
        .where(*filters)
        .order_by(Deployment.deployed_at.desc(), Deployment.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(items_stmt).all()

    items = [
        DeploymentRead(
            id=dep.id,
            service_id=dep.service_id,
            service_name=svc_name,
            version=dep.version,
            commit_sha=dep.commit_sha,
            status=dep.status,
            deployed_by=dep.deployed_by,
            deployed_at=dep.deployed_at,
            metadata_json=dep.metadata_json,
        )
        for dep, svc_name in rows
    ]

    return DeploymentListResponse(total=total, limit=limit, offset=offset, items=items)
