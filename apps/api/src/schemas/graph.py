from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.graph import RelationType, SignalType
from services.ingestion.normalizer import normalize_service_name, normalize_timestamp_utc


class ServiceEdgeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    caller: str
    callee: str
    call_count: int
    error_count: int
    error_rate: float
    avg_duration_ms: float


class DependencyGraphResponse(BaseModel):
    node_count: int
    edge_count: int
    nodes: list[str]
    edges: list[ServiceEdgeResponse]


class HopServiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    service: str
    hop_distance: int


class ServiceImpactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    target_service: str
    max_depth: int
    dependencies: list[HopServiceResponse]
    affected_callers: list[HopServiceResponse]
    all_related_services: list[str]


class CorrelationTimelineRequest(BaseModel):
    service: str
    start_time: datetime
    end_time: datetime
    lookback_minutes: int = Field(default=60, ge=0, le=1440)
    max_depth: int = Field(default=3, ge=1, le=10)

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
    def _validate_window(self) -> "CorrelationTimelineRequest":
        if self.start_time > self.end_time:
            raise ValueError("start_time must be less than or equal to end_time")
        return self


class TimelineItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    timestamp_utc: datetime
    signal_type: SignalType
    service: str
    relation: RelationType
    hop_distance: int
    summary: str
    record_id: str | None = None
    minutes_relative_to_window_start: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CorrelationTimelineResponse(BaseModel):
    target_service: str
    window_start: datetime
    window_end: datetime
    lookback_minutes: int
    deployment_window_start: datetime
    max_depth: int
    related_services: list[str]
    correlated_services: list[str]
    impact: ServiceImpactResponse
    deployment_count: int
    anomaly_count: int
    event_count: int
    incident_count: int
    timeline: list[TimelineItemResponse]
    correlation_note: str
