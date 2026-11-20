import csv
import io
import json
import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from services.ingestion.normalizer import (
    compute_metric_dedup_hash,
    normalize_deployment_status,
    normalize_metric_name,
    normalize_service_name,
    normalize_span_kind,
    normalize_span_status,
    normalize_timestamp_utc,
)
from services.ingestion.validator import (
    ValidationFailure,
    _format_validation_error,
    _normalize_optional_id,
    _sanitize_for_jsonb,
)

REQUIRED_METRIC_CSV_COLUMNS: frozenset[str] = frozenset(
    {"service", "metric_name", "timestamp", "value"}
)
_MAX_UNIT_LENGTH = 32
_MAX_TRACE_ID_LENGTH = 128
_MAX_SPAN_ID_LENGTH = 64
_MAX_OPERATION_NAME_LENGTH = 256
_MAX_VERSION_LENGTH = 128
_MAX_COMMIT_SHA_LENGTH = 64
_MAX_DEPLOYED_BY_LENGTH = 128


class CsvFormatError(ValueError):
    """Raised when a CSV payload is empty, malformed at header level, or exceeds row limits."""


def _validate_required_str(value: Any, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} cannot be empty")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} exceeds maximum length of {max_length} characters")
    return cleaned


def _validate_finite_float(value: Any, *, field_name: str, allow_negative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number")
    if not allow_negative and result < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return result


def _validate_json_dict(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a dict")
    if not all(isinstance(k, str) for k in value):
        raise ValueError(f"{field_name} keys must be strings")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-serializable without NaN/Infinity") from exc
    return dict(value)


class RawMetricPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: str
    metric_name: str
    timestamp: datetime
    value: float
    unit: str | None = None
    labels: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service", mode="before")
    @classmethod
    def _validate_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("metric_name", mode="before")
    @classmethod
    def _validate_metric_name(cls, value: Any) -> str:
        return normalize_metric_name(value)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)

    @field_validator("value", mode="before")
    @classmethod
    def _validate_value(cls, value: Any) -> float:
        return _validate_finite_float(value, field_name="value", allow_negative=True)

    @field_validator("unit", mode="before")
    @classmethod
    def _validate_unit(cls, value: Any) -> str | None:
        return _normalize_optional_id(value, field_name="unit", max_length=_MAX_UNIT_LENGTH)

    @field_validator("labels", mode="before")
    @classmethod
    def _validate_labels(cls, value: Any) -> dict[str, Any]:
        return _validate_json_dict(value, field_name="labels")


class NormalizedMetricPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    record_index: int
    service_name: str
    metric_name: str
    timestamp_utc: datetime
    value: float
    unit: str | None = None
    labels: dict[str, Any] = Field(default_factory=dict)
    dedup_hash: str


class MetricBatchValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid_points: list[NormalizedMetricPoint] = Field(default_factory=list)
    failures: list[ValidationFailure] = Field(default_factory=list)


def validate_and_normalize_metric_batch(records: Sequence[Any]) -> MetricBatchValidationResult:
    """Validate and normalize a batch of raw metric point payloads independently."""
    valid_points: list[NormalizedMetricPoint] = []
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
            raw = RawMetricPoint.model_validate(item)
            dedup_hash = compute_metric_dedup_hash(
                service_name=raw.service,
                metric_name=raw.metric_name,
                timestamp_utc=raw.timestamp,
                labels=raw.labels,
            )
            valid_points.append(
                NormalizedMetricPoint(
                    record_index=index,
                    service_name=raw.service,
                    metric_name=raw.metric_name,
                    timestamp_utc=raw.timestamp,
                    value=raw.value,
                    unit=raw.unit,
                    labels=raw.labels,
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

    return MetricBatchValidationResult(valid_points=valid_points, failures=failures)


def parse_and_validate_metrics_csv(
    csv_text: str,
    *,
    max_rows: int = 5000,
) -> MetricBatchValidationResult:
    """Parse UTF-8 CSV metric rows using stdlib csv.DictReader and validate per row."""
    cleaned_text = csv_text.lstrip("\ufeff")
    if not cleaned_text.strip():
        raise CsvFormatError("CSV payload cannot be empty")

    reader = csv.DictReader(io.StringIO(cleaned_text))
    if not reader.fieldnames:
        raise CsvFormatError("CSV payload is missing header row")

    normalized_headers = [h.strip() for h in reader.fieldnames if h is not None]
    missing = sorted(REQUIRED_METRIC_CSV_COLUMNS - set(normalized_headers))
    if missing:
        raise CsvFormatError(f"CSV missing required column(s): {', '.join(missing)}")

    rows = list(reader)
    if not rows:
        raise CsvFormatError("CSV payload contains no data rows")
    if len(rows) > max_rows:
        raise CsvFormatError(f"CSV payload exceeds maximum of {max_rows} rows")

    valid_points: list[NormalizedMetricPoint] = []
    failures: list[ValidationFailure] = []

    for index, raw_row in enumerate(rows):
        row = {
            (k.strip() if isinstance(k, str) else "__extra__"): v for k, v in raw_row.items()
        }
        raw_payload = _sanitize_for_jsonb(row)

        raw_value = row.get("value")
        if not isinstance(raw_value, str) or not raw_value.strip():
            failures.append(
                ValidationFailure(
                    record_index=index,
                    error_code="VALIDATION_ERROR",
                    error_detail="value: numeric value is required",
                    raw_payload=raw_payload,
                )
            )
            continue

        try:
            numeric_value = float(raw_value.strip())
        except ValueError:
            failures.append(
                ValidationFailure(
                    record_index=index,
                    error_code="VALIDATION_ERROR",
                    error_detail=f"value: invalid float {raw_value!r}",
                    raw_payload=raw_payload,
                )
            )
            continue

        raw_labels = row.get("labels")
        parsed_labels: Any = {}
        if isinstance(raw_labels, str) and raw_labels.strip():
            try:
                parsed_labels = json.loads(raw_labels)
            except json.JSONDecodeError as exc:
                failures.append(
                    ValidationFailure(
                        record_index=index,
                        error_code="VALIDATION_ERROR",
                        error_detail=f"labels: invalid JSON ({exc.msg})",
                        raw_payload=raw_payload,
                    )
                )
                continue

        candidate: dict[str, Any] = {
            "service": row.get("service"),
            "metric_name": row.get("metric_name"),
            "timestamp": row.get("timestamp"),
            "value": numeric_value,
            "unit": row.get("unit"),
            "labels": parsed_labels,
        }

        try:
            parsed = RawMetricPoint.model_validate(candidate)
            dedup_hash = compute_metric_dedup_hash(
                service_name=parsed.service,
                metric_name=parsed.metric_name,
                timestamp_utc=parsed.timestamp,
                labels=parsed.labels,
            )
            valid_points.append(
                NormalizedMetricPoint(
                    record_index=index,
                    service_name=parsed.service,
                    metric_name=parsed.metric_name,
                    timestamp_utc=parsed.timestamp,
                    value=parsed.value,
                    unit=parsed.unit,
                    labels=parsed.labels,
                    dedup_hash=dedup_hash,
                )
            )
        except ValidationError as exc:
            failures.append(
                ValidationFailure(
                    record_index=index,
                    error_code="VALIDATION_ERROR",
                    error_detail=_format_validation_error(exc),
                    raw_payload=raw_payload,
                )
            )

    return MetricBatchValidationResult(valid_points=valid_points, failures=failures)


class RawTraceSpan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    operation_name: str
    span_kind: str = "INTERNAL"
    status_code: str = "UNSET"
    start_time: datetime
    end_time: datetime
    duration_ms: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service", mode="before")
    @classmethod
    def _validate_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("trace_id", mode="before")
    @classmethod
    def _validate_trace_id(cls, value: Any) -> str:
        return _validate_required_str(value, field_name="trace_id", max_length=_MAX_TRACE_ID_LENGTH)

    @field_validator("span_id", mode="before")
    @classmethod
    def _validate_span_id(cls, value: Any) -> str:
        return _validate_required_str(value, field_name="span_id", max_length=_MAX_SPAN_ID_LENGTH)

    @field_validator("parent_span_id", mode="before")
    @classmethod
    def _validate_parent_span_id(cls, value: Any) -> str | None:
        return _normalize_optional_id(
            value, field_name="parent_span_id", max_length=_MAX_SPAN_ID_LENGTH
        )

    @field_validator("operation_name", mode="before")
    @classmethod
    def _validate_operation_name(cls, value: Any) -> str:
        return _validate_required_str(
            value, field_name="operation_name", max_length=_MAX_OPERATION_NAME_LENGTH
        )

    @field_validator("span_kind", mode="before")
    @classmethod
    def _validate_span_kind(cls, value: Any) -> str:
        return normalize_span_kind(value)

    @field_validator("status_code", mode="before")
    @classmethod
    def _validate_status_code(cls, value: Any) -> str:
        return normalize_span_status(value)

    @field_validator("start_time", mode="before")
    @classmethod
    def _validate_start_time(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)

    @field_validator("end_time", mode="before")
    @classmethod
    def _validate_end_time(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)

    @field_validator("duration_ms", mode="before")
    @classmethod
    def _validate_duration_ms(cls, value: Any) -> float | None:
        if value is None:
            return None
        return _validate_finite_float(value, field_name="duration_ms", allow_negative=False)

    @field_validator("attributes", mode="before")
    @classmethod
    def _validate_attributes(cls, value: Any) -> dict[str, Any]:
        return _validate_json_dict(value, field_name="attributes")

    @model_validator(mode="after")
    def _check_times_and_duration(self) -> "RawTraceSpan":
        if self.end_time < self.start_time:
            raise ValueError("end_time must be greater than or equal to start_time")
        if self.duration_ms is None:
            self.duration_ms = (self.end_time - self.start_time).total_seconds() * 1000.0
        return self


class NormalizedTraceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    record_index: int
    service_name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    operation_name: str
    span_kind: str
    status_code: str
    start_time_utc: datetime
    end_time_utc: datetime
    duration_ms: float
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceBatchValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid_spans: list[NormalizedTraceSpan] = Field(default_factory=list)
    failures: list[ValidationFailure] = Field(default_factory=list)


def validate_and_normalize_trace_batch(records: Sequence[Any]) -> TraceBatchValidationResult:
    """Validate and normalize a batch of raw trace span payloads independently."""
    valid_spans: list[NormalizedTraceSpan] = []
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
            raw = RawTraceSpan.model_validate(item)
            assert raw.duration_ms is not None
            valid_spans.append(
                NormalizedTraceSpan(
                    record_index=index,
                    service_name=raw.service,
                    trace_id=raw.trace_id,
                    span_id=raw.span_id,
                    parent_span_id=raw.parent_span_id,
                    operation_name=raw.operation_name,
                    span_kind=raw.span_kind,
                    status_code=raw.status_code,
                    start_time_utc=raw.start_time,
                    end_time_utc=raw.end_time,
                    duration_ms=raw.duration_ms,
                    attributes=raw.attributes,
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

    return TraceBatchValidationResult(valid_spans=valid_spans, failures=failures)


class RawDeployment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: str
    version: str
    deployed_at: datetime
    status: str
    commit_sha: str | None = None
    deployed_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service", mode="before")
    @classmethod
    def _validate_service(cls, value: Any) -> str:
        return normalize_service_name(value)

    @field_validator("version", mode="before")
    @classmethod
    def _validate_version(cls, value: Any) -> str:
        return _validate_required_str(value, field_name="version", max_length=_MAX_VERSION_LENGTH)

    @field_validator("deployed_at", mode="before")
    @classmethod
    def _validate_deployed_at(cls, value: Any) -> datetime:
        return normalize_timestamp_utc(value)

    @field_validator("status", mode="before")
    @classmethod
    def _validate_status(cls, value: Any) -> str:
        return normalize_deployment_status(value)

    @field_validator("commit_sha", mode="before")
    @classmethod
    def _validate_commit_sha(cls, value: Any) -> str | None:
        return _normalize_optional_id(
            value, field_name="commit_sha", max_length=_MAX_COMMIT_SHA_LENGTH
        )

    @field_validator("deployed_by", mode="before")
    @classmethod
    def _validate_deployed_by(cls, value: Any) -> str | None:
        return _normalize_optional_id(
            value, field_name="deployed_by", max_length=_MAX_DEPLOYED_BY_LENGTH
        )

    @field_validator("metadata", mode="before")
    @classmethod
    def _validate_metadata(cls, value: Any) -> dict[str, Any]:
        return _validate_json_dict(value, field_name="metadata")


class NormalizedDeployment(BaseModel):
    model_config = ConfigDict(frozen=True)

    record_index: int
    service_name: str
    version: str
    commit_sha: str | None = None
    status: str
    deployed_by: str | None = None
    deployed_at: datetime
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class DeploymentBatchValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid_deployments: list[NormalizedDeployment] = Field(default_factory=list)
    failures: list[ValidationFailure] = Field(default_factory=list)


def validate_and_normalize_deployment_batch(
    records: Sequence[Any],
) -> DeploymentBatchValidationResult:
    """Validate and normalize a batch of raw deployment payloads independently."""
    valid_deployments: list[NormalizedDeployment] = []
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
            raw = RawDeployment.model_validate(item)
            valid_deployments.append(
                NormalizedDeployment(
                    record_index=index,
                    service_name=raw.service,
                    version=raw.version,
                    commit_sha=raw.commit_sha,
                    status=raw.status,
                    deployed_by=raw.deployed_by,
                    deployed_at=raw.deployed_at,
                    metadata_json=raw.metadata,
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

    return DeploymentBatchValidationResult(
        valid_deployments=valid_deployments,
        failures=failures,
    )
