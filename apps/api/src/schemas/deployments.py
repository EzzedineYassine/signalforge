from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.src.schemas.events import IngestionErrorItem


class DeploymentIngestRequest(BaseModel):
    deployments: list[Any] = Field(..., min_length=1, max_length=1000)


class DeploymentIngestResponse(BaseModel):
    batch_id: UUID
    received: int
    accepted: int
    duplicates: int
    rejected: int
    errors: list[IngestionErrorItem]


class DeploymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    service_name: str
    version: str
    commit_sha: str | None
    status: str
    deployed_by: str | None
    deployed_at: datetime
    metadata_json: dict[str, Any]


class DeploymentListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[DeploymentRead]
