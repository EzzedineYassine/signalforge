import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.ingestion.normalizer import normalize_service_name, normalize_timestamp_utc

SignalType = Literal["deployment", "anomaly", "event", "incident"]
RelationType = Literal["target", "dependency", "affected_caller", "bidirectional"]

DEFAULT_CORRELATION_NOTE = (
    "Observed temporal and topological correlations only; causation is not established."
)


class ServiceEdgeInput(BaseModel):
    """Raw directed service-to-service call observation (caller -> callee)."""

    model_config = ConfigDict(frozen=True)

    caller: str
    callee: str
    call_count: int = Field(default=1, ge=1)
    error_count: int = Field(default=0, ge=0)
    avg_duration_ms: float = Field(default=0.0, ge=0.0)

    @field_validator("caller", "callee", mode="before")
    @classmethod
    def _normalize_endpoint(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("avg_duration_ms")
    @classmethod
    def _validate_finite_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("avg_duration_ms must be a finite number")
        return float(value)

    @model_validator(mode="after")
    def _validate_error_bounds(self) -> "ServiceEdgeInput":
        if self.error_count > self.call_count:
            raise ValueError(
                f"error_count ({self.error_count}) cannot exceed call_count ({self.call_count})"
            )
        return self


class ServiceEdge(BaseModel):
    """Aggregated directed dependency edge (caller -> callee)."""

    model_config = ConfigDict(frozen=True)

    caller: str
    callee: str
    call_count: int = Field(ge=1)
    error_count: int = Field(ge=0)
    error_rate: float = Field(ge=0.0, le=1.0)
    avg_duration_ms: float = Field(ge=0.0)


class HopService(BaseModel):
    """Service reachable at a specific shortest-path hop distance."""

    model_config = ConfigDict(frozen=True)

    service: str
    hop_distance: int = Field(ge=1)


class ImpactAnalysis(BaseModel):
    """Multi-hop upstream/downstream dependency topology around a target service."""

    model_config = ConfigDict(frozen=True)

    target_service: str
    max_depth: int = Field(ge=1)
    dependencies: list[HopService] = Field(default_factory=list)
    affected_callers: list[HopService] = Field(default_factory=list)
    all_related_services: list[str] = Field(default_factory=list)


class DeploymentRecord(BaseModel):
    """Deployment signal input for timeline correlation."""

    model_config = ConfigDict(frozen=True)

    service: str
    timestamp_utc: datetime
    version: str
    status: str = "succeeded"
    deployment_id: str | None = None
    commit_sha: str | None = None
    environment: str = "production"

    @field_validator("service", mode="before")
    @classmethod
    def _normalize_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("timestamp_utc", mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)


class AnomalyRecord(BaseModel):
    """Anomaly detection signal input for timeline correlation."""

    model_config = ConfigDict(frozen=True)

    service: str
    timestamp_utc: datetime
    detector_name: str
    score: float
    threshold: float
    is_anomaly: bool = True
    anomaly_id: str | None = None
    metric_names: list[str] = Field(default_factory=list)

    @field_validator("service", mode="before")
    @classmethod
    def _normalize_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("timestamp_utc", mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)


class EventRecord(BaseModel):
    """Operational event/log signal input for timeline correlation."""

    model_config = ConfigDict(frozen=True)

    service: str
    timestamp_utc: datetime
    message: str
    severity: str = "ERROR"
    event_type: str = "log"
    event_id: str | None = None
    trace_id: str | None = None

    @field_validator("service", mode="before")
    @classmethod
    def _normalize_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("timestamp_utc", mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)


class IncidentRecord(BaseModel):
    """Incident signal input for timeline correlation."""

    model_config = ConfigDict(frozen=True)

    service: str
    timestamp_utc: datetime
    title: str
    severity: str = "CRITICAL"
    status: str = "open"
    incident_id: str | None = None

    @field_validator("service", mode="before")
    @classmethod
    def _normalize_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("timestamp_utc", mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)


class TimelineItem(BaseModel):
    """Single chronologically ordered signal inside an incident correlation timeline."""

    model_config = ConfigDict(frozen=True)

    timestamp_utc: datetime
    signal_type: SignalType
    service: str
    relation: RelationType
    hop_distance: int = Field(ge=0)
    summary: str
    record_id: str | None = None
    minutes_relative_to_window_start: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CorrelationSummary(BaseModel):
    """Topological impact and chronological multi-signal correlation summary."""

    model_config = ConfigDict(frozen=True)

    target_service: str
    window_start: datetime
    window_end: datetime
    lookback_minutes: int = Field(ge=0)
    deployment_window_start: datetime
    max_depth: int = Field(ge=1)
    impact: ImpactAnalysis
    correlated_services: list[str]
    counts_by_signal: dict[SignalType, int]
    timeline: list[TimelineItem]
    correlation_note: str = DEFAULT_CORRELATION_NOTE
