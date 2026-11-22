from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.models import Investigation
from apps.api.src.routes import parse_service_filter
from apps.api.src.schemas.investigations import (
    InvestigationListResponse,
    InvestigationRead,
    InvestigationRunRequest,
)
from apps.api.src.services.anomaly_service import ServiceNotFoundError
from apps.api.src.services.investigation_service import (
    InvestigationNotFoundError,
    get_investigation_by_id,
    get_llm_client,
    list_investigations,
    run_and_persist_investigation,
)
from apps.api.src.services.knowledge_service import get_knowledge_retriever
from services.investigation import InvestigationReport, LLMClient
from services.retrieval.search.hybrid import HybridRetriever

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])


def _to_investigation_read(row: Investigation, service_name: str) -> InvestigationRead:
    report = InvestigationReport.model_validate(row.report_json)
    return InvestigationRead(
        id=row.id,
        service_id=row.service_id,
        service_name=service_name,
        created_at=row.created_at,
        executed_nodes=list(row.executed_nodes or []),
        duration_ms=float(row.duration_ms),
        abstained=bool(row.abstained),
        model_name=row.model_name,
        report=report,
    )


@router.post("/run", response_model=InvestigationRead, status_code=status.HTTP_200_OK)
def run_investigation(
    payload: InvestigationRunRequest,
    db: Annotated[Session, Depends(get_db)],
    llm_client: Annotated[LLMClient, Depends(get_llm_client)],
    retriever: Annotated[HybridRetriever, Depends(get_knowledge_retriever)],
) -> InvestigationRead:
    try:
        row, service_name = run_and_persist_investigation(
            db,
            question=payload.question,
            service=payload.service,
            start_time=payload.start_time,
            end_time=payload.end_time,
            lookback_minutes=payload.lookback_minutes,
            max_depth=payload.max_depth,
            top_k_docs=payload.top_k_docs,
            llm_client=llm_client,
            retriever=retriever,
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
            detail="Database error while running investigation",
        ) from exc

    return _to_investigation_read(row, service_name)


@router.get("", response_model=InvestigationListResponse, status_code=status.HTTP_200_OK)
def read_investigations(
    db: Annotated[Session, Depends(get_db)],
    service: str | None = None,
    abstained: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InvestigationListResponse:
    service_name = parse_service_filter(service)
    try:
        total, rows = list_investigations(
            db,
            service=service_name,
            abstained=abstained,
            limit=limit,
            offset=offset,
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while listing investigations",
        ) from exc

    return InvestigationListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[_to_investigation_read(row, svc_name) for row, svc_name in rows],
    )


@router.get(
    "/{investigation_id}",
    response_model=InvestigationRead,
    status_code=status.HTTP_200_OK,
)
def read_investigation(
    investigation_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvestigationRead:
    try:
        row, service_name = get_investigation_by_id(db, investigation_id)
    except InvestigationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while retrieving investigation",
        ) from exc

    return _to_investigation_read(row, service_name)
