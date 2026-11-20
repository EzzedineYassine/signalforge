from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.src.schemas.events import IngestionErrorItem


class TraceIngestRequest(BaseModel):
    spans: list[Any] = Field(..., min_length=1, max_length=5000)


class TraceIngestResponse(BaseModel):
    batch_id: UUID
    received: int
    accepted: int
    duplicates: int
    rejected: int
    errors: list[IngestionErrorItem]


class TraceSpanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    service_name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    operation_name: str
    span_kind: str
    status_code: str
    start_time_utc: datetime
    end_time_utc: datetime
    duration_ms: float
    attributes: dict[str, Any]
    ingested_at: datetime


class TraceListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TraceSpanRead]
