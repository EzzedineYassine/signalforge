from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class IncidentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str | None
    title: str
    description: str | None
    service_id: UUID | None
    service_name: str | None
    severity: str
    status: str
    started_at: datetime
    resolved_at: datetime | None
    created_at: datetime


class IncidentListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[IncidentRead]
