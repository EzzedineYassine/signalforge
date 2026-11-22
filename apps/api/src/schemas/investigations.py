from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from services.ingestion.normalizer import normalize_service_name, normalize_timestamp_utc
from services.investigation import InvestigationReport


class InvestigationRunRequest(BaseModel):
    question: str
    service: str
    start_time: datetime
    end_time: datetime
    lookback_minutes: int = Field(default=60, ge=0, le=1440)
    max_depth: int = Field(default=3, ge=1, le=10)
    top_k_docs: int = Field(default=5, ge=1, le=20)

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question cannot be empty")
        return cleaned

    @field_validator("service", mode="before")
    @classmethod
    def _validate_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("start_time", mode="before")
    @classmethod
    def _validate_start_time(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)

    @field_validator("end_time", mode="before")
    @classmethod
    def _validate_end_time(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)

    @model_validator(mode="after")
    def _validate_window(self) -> "InvestigationRunRequest":
        if self.start_time > self.end_time:
            raise ValueError("start_time must be less than or equal to end_time")
        return self


class InvestigationRead(BaseModel):
    id: UUID
    service_id: UUID
    service_name: str
    created_at: datetime
    executed_nodes: list[str]
    duration_ms: float
    abstained: bool
    model_name: str
    report: InvestigationReport


class InvestigationListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[InvestigationRead]
