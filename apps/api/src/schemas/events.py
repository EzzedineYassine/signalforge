from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


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
