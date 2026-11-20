from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.src.core.database import get_db
from apps.api.src.schemas.events import (
    EventIngestRequest,
    EventIngestResponse,
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
