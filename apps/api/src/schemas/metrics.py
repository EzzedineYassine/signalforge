from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.src.schemas.events import IngestionErrorItem


class MetricIngestRequest(BaseModel):
    metrics: list[Any] = Field(..., min_length=1, max_length=5000)


class MetricIngestResponse(BaseModel):
    batch_id: UUID
    received: int
    accepted: int
    duplicates: int
    rejected: int
    errors: list[IngestionErrorItem]


class MetricPointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    service_name: str
    metric_name: str
    timestamp_utc: datetime
    value: float
    unit: str | None
    labels: dict[str, Any]
    dedup_hash: str
    ingested_at: datetime


class MetricListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[MetricPointRead]
