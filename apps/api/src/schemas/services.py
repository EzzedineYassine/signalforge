from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ServiceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    environment: str
    owner_team: str | None
    is_auto_discovered: bool
    event_count: int
    last_seen_at: datetime | None
    created_at: datetime


class ServiceListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ServiceSummary]
