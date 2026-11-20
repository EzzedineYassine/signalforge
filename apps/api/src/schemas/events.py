from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventIngestRequest(BaseModel):
    events: list[Any] = Field(..., min_length=1, max_length=1000)


class IngestionErrorItem(BaseModel):
    index: int
    error_code: str
    error_detail: str


class EventIngestResponse(BaseModel):
    batch_id: UUID
    received: int
    accepted: int
    duplicates: int
    rejected: int
    errors: list[IngestionErrorItem]


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    service_name: str
    external_id: str | None
    dedup_hash: str
    timestamp_utc: datetime
    event_type: str
    severity: str
    message: str
    trace_id: str | None
    span_id: str | None
    attributes: dict[str, Any]
    ingested_at: datetime


class EventListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[EventRead]
