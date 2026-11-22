from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.routes import parse_time_range
from apps.api.src.schemas.graph import (
    CorrelationTimelineRequest,
    CorrelationTimelineResponse,
    DependencyGraphResponse,
    ServiceEdgeResponse,
    ServiceImpactResponse,
    TimelineItemResponse,
)
from apps.api.src.services.anomaly_service import ServiceNotFoundError
from apps.api.src.services.graph_service import (
    build_correlation_timeline,
    get_dependency_graph,
    get_service_impact,
)
from services.ingestion.normalizer import normalize_service_name

router = APIRouter(prefix="/api/v1", tags=["graph"])


@router.get(
    "/graph/dependencies",
    response_model=DependencyGraphResponse,
    status_code=status.HTTP_200_OK,
)
def read_dependency_graph(
    db: Annotated[Session, Depends(get_db)],
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> DependencyGraphResponse:
    start_utc, end_utc = parse_time_range(start_time, end_time)
    try:
        graph_result = get_dependency_graph(db, start_time=start_utc, end_time=end_utc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while building dependency graph",
        ) from exc

    edges = [ServiceEdgeResponse.model_validate(edge) for edge in graph_result.edges]
    return DependencyGraphResponse(
        node_count=len(graph_result.nodes),
        edge_count=len(edges),
        nodes=graph_result.nodes,
        edges=edges,
    )


@router.get(
    "/graph/services/{service}/impact",
    response_model=ServiceImpactResponse,
    status_code=status.HTTP_200_OK,
)
def read_service_impact(
    service: str,
    db: Annotated[Session, Depends(get_db)],
    max_depth: Annotated[int, Query(ge=1, le=10)] = 3,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> ServiceImpactResponse:
    try:
        normalized_service = normalize_service_name(service)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid service name: {exc}",
        ) from exc

    start_utc, end_utc = parse_time_range(start_time, end_time)

    try:
        impact = get_service_impact(
            db,
            service=normalized_service,
            max_depth=max_depth,
            start_time=start_utc,
            end_time=end_utc,
        )
    except ServiceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while computing service impact",
        ) from exc

    return ServiceImpactResponse.model_validate(impact)


@router.post(
    "/correlation/timeline",
    response_model=CorrelationTimelineResponse,
    status_code=status.HTTP_200_OK,
)
def create_correlation_timeline(
    payload: CorrelationTimelineRequest,
    db: Annotated[Session, Depends(get_db)],
) -> CorrelationTimelineResponse:
    try:
        summary = build_correlation_timeline(
            db,
            target_service=payload.service,
            window_start=payload.start_time,
            window_end=payload.end_time,
            lookback_minutes=payload.lookback_minutes,
            max_depth=payload.max_depth,
        )
    except ServiceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while building correlation timeline",
        ) from exc

    return CorrelationTimelineResponse(
        target_service=summary.target_service,
        window_start=summary.window_start,
        window_end=summary.window_end,
        lookback_minutes=summary.lookback_minutes,
        deployment_window_start=summary.deployment_window_start,
        max_depth=summary.max_depth,
        related_services=list(summary.impact.all_related_services),
        correlated_services=list(summary.correlated_services),
        impact=ServiceImpactResponse.model_validate(summary.impact),
        deployment_count=summary.counts_by_signal["deployment"],
        anomaly_count=summary.counts_by_signal["anomaly"],
        event_count=summary.counts_by_signal["event"],
        incident_count=summary.counts_by_signal["incident"],
        timeline=[TimelineItemResponse.model_validate(item) for item in summary.timeline],
        correlation_note=summary.correlation_note,
    )
