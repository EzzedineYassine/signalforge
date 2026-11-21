from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from apps.api.src.services.anomaly_service import DetectorName
from services.ingestion.normalizer import normalize_service_name, normalize_timestamp_utc


class AnomalyDetectRequest(BaseModel):
    service: str
    start_time: datetime
    end_time: datetime
    detector: DetectorName = "lstm_autoencoder"
    persist_all: bool = False

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
    def _validate_range(self) -> "AnomalyDetectRequest":
        if self.start_time > self.end_time:
            raise ValueError("start_time must be less than or equal to end_time")
        return self


class AnomalyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    service_name: str
    detector_name: str
    timestamp_utc: datetime
    score: float
    threshold: float
    is_anomaly: bool
    metric_names: list[str]
    details: dict[str, Any]
    dedup_hash: str
    detected_at: datetime


class AnomalyDetectResponse(BaseModel):
    service: str
    detector: DetectorName
    points_evaluated: int
    anomalies_detected: int
    persisted: int
    duplicates: int
    threshold: float
    max_score: float
    items: list[AnomalyRead]


class AnomalyListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AnomalyRead]
