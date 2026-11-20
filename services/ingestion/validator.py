import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.ingestion.normalizer import (
    compute_dedup_hash,
    normalize_event_type,
    normalize_service_name,
    normalize_severity,
    normalize_timestamp_utc,
)

_MAX_EXTERNAL_ID_LENGTH = 256
_MAX_TRACE_ID_LENGTH = 128
_MAX_SPAN_ID_LENGTH = 64


def _normalize_optional_id(value: Any, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} exceeds maximum length of {max_length} characters")
    return cleaned


def _sanitize_for_jsonb(value: Any) -> Any:
    """Convert arbitrary Python values into a PostgreSQL JSONB-safe structure."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _sanitize_for_jsonb(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_jsonb(v) for v in value]
    return repr(value)


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(segment) for segment in err.get("loc", ()))
        msg = err.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


class RawEvent(BaseModel):
    """Schema for validating and normalizing a single raw event payload."""

    model_config = ConfigDict(extra="ignore")

    service: str
    timestamp: datetime
    message: str
    event_type: str = "log"
    severity: str = "INFO"
    external_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service", mode="before")
    @classmethod
    def _validate_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)

    @field_validator("message", mode="before")
    @classmethod
    def _validate_message(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("message must be a string")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message cannot be empty")
        return cleaned

    @field_validator("event_type", mode="before")
    @classmethod
    def _validate_event_type(cls, value: Any) -> str:
        return normalize_event_type(value)

    @field_validator("severity", mode="before")
    @classmethod
    def _validate_severity(cls, value: Any) -> str:
        return normalize_severity(value)

    @field_validator("external_id", mode="before")
    @classmethod
    def _validate_external_id(cls, value: Any) -> str | None:
        return _normalize_optional_id(
            value, field_name="external_id", max_length=_MAX_EXTERNAL_ID_LENGTH
        )

    @field_validator("trace_id", mode="before")
    @classmethod
    def _validate_trace_id(cls, value: Any) -> str | None:
        return _normalize_optional_id(
            value, field_name="trace_id", max_length=_MAX_TRACE_ID_LENGTH
        )

    @field_validator("span_id", mode="before")
    @classmethod
    def _validate_span_id(cls, value: Any) -> str | None:
        return _normalize_optional_id(
            value, field_name="span_id", max_length=_MAX_SPAN_ID_LENGTH
        )

    @field_validator("attributes", mode="before")
    @classmethod
    def _validate_attributes(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("attributes must be a dict")
        if not all(isinstance(k, str) for k in value):
            raise ValueError("attributes keys must be strings")
        return dict(value)


class NormalizedEvent(BaseModel):
    """Immutable normalized event ready for persistence."""

    model_config = ConfigDict(frozen=True)

    record_index: int
    service_name: str
    timestamp_utc: datetime
    event_type: str
    severity: str
    message: str
    external_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    dedup_hash: str


class ValidationFailure(BaseModel):
    """Structured per-record failure matching the ingestion_failures table."""

    model_config = ConfigDict(frozen=True)

    record_index: int
    error_code: str
    error_detail: str
    raw_payload: dict[str, Any]


class BatchValidationResult(BaseModel):
    """Result of validating and normalizing an incoming batch of raw records."""

    model_config = ConfigDict(frozen=True)

    valid_events: list[NormalizedEvent] = Field(default_factory=list)
    failures: list[ValidationFailure] = Field(default_factory=list)


def validate_and_normalize_batch(records: Sequence[Any]) -> BatchValidationResult:
    """Validate and normalize each record independently without failing the batch."""
    valid_events: list[NormalizedEvent] = []
    failures: list[ValidationFailure] = []

    for index, item in enumerate(records):
        if not isinstance(item, dict):
            failures.append(
                ValidationFailure(
                    record_index=index,
                    error_code="INVALID_RECORD_TYPE",
                    error_detail=f"Expected a dict record, got {type(item).__name__}",
                    raw_payload={"raw": repr(item)},
                )
            )
            continue

        try:
            raw_event = RawEvent.model_validate(item)
            dedup_hash = compute_dedup_hash(
                service_name=raw_event.service,
                timestamp_utc=raw_event.timestamp,
                event_type=raw_event.event_type,
                severity=raw_event.severity,
                message=raw_event.message,
                external_id=raw_event.external_id,
                trace_id=raw_event.trace_id,
                span_id=raw_event.span_id,
            )
            valid_events.append(
                NormalizedEvent(
                    record_index=index,
                    service_name=raw_event.service,
                    timestamp_utc=raw_event.timestamp,
                    event_type=raw_event.event_type,
                    severity=raw_event.severity,
                    message=raw_event.message,
                    external_id=raw_event.external_id,
                    trace_id=raw_event.trace_id,
                    span_id=raw_event.span_id,
                    attributes=raw_event.attributes,
                    dedup_hash=dedup_hash,
                )
            )
        except ValidationError as exc:
            failures.append(
                ValidationFailure(
                    record_index=index,
                    error_code="VALIDATION_ERROR",
                    error_detail=_format_validation_error(exc),
                    raw_payload=_sanitize_for_jsonb(item),
                )
            )
        except Exception as exc:
            failures.append(
                ValidationFailure(
                    record_index=index,
                    error_code="UNEXPECTED_ERROR",
                    error_detail=f"{type(exc).__name__}: {exc}",
                    raw_payload=_sanitize_for_jsonb(item),
                )
            )

    return BatchValidationResult(valid_events=valid_events, failures=failures)
