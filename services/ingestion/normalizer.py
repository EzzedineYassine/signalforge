import hashlib
import math
import re
from datetime import UTC, datetime

_SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}\Z")
_SERVICE_SEP_RE = re.compile(r"[_. ]+")
_MULTI_HYPHEN_RE = re.compile(r"-{2,}")

CANONICAL_SEVERITIES: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)

SEVERITY_ALIASES: dict[str, str] = {
    "TRACE": "DEBUG",
    "WARN": "WARNING",
    "ERR": "ERROR",
    "CRIT": "CRITICAL",
    "FATAL": "CRITICAL",
}

_EPOCH_MS_THRESHOLD = 1e12
_MAX_EVENT_TYPE_LENGTH = 64


def normalize_service_name(name: str) -> str:
    """Normalize a service identifier to lowercase kebab-case matching VARCHAR(128)."""
    if not isinstance(name, str):
        raise ValueError("service name must be a string")

    cleaned = name.strip().lower()
    cleaned = _SERVICE_SEP_RE.sub("-", cleaned)
    cleaned = _MULTI_HYPHEN_RE.sub("-", cleaned).strip("-")

    if not _SERVICE_NAME_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid service name {name!r}: must normalize to ^[a-z0-9][a-z0-9-]{{0,127}}$"
        )
    return cleaned


def normalize_timestamp_utc(value: datetime | str | int | float) -> datetime:
    """Normalize an explicit-timezone datetime, ISO-8601 string, or Unix epoch to UTC.

    Rejects naive datetimes/strings, booleans, non-finite numbers, and negative epochs.
    """
    if isinstance(value, bool):
        raise ValueError("Timestamp cannot be a boolean")

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Naive datetime is not allowed; timezone offset is required")
        return value.astimezone(UTC)

    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("Timestamp must be a finite number")
        if value < 0:
            raise ValueError("Negative Unix epoch timestamps are not allowed")
        try:
            if value >= _EPOCH_MS_THRESHOLD:
                if isinstance(value, int):
                    seconds, millis = divmod(value, 1000)
                    return datetime.fromtimestamp(seconds, tz=UTC).replace(
                        microsecond=millis * 1000
                    )
                return datetime.fromtimestamp(value / 1000.0, tz=UTC)
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"Unix timestamp out of supported range: {value!r}") from exc

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("Timestamp string cannot be empty")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(
                f"Naive ISO-8601 timestamp is not allowed; timezone offset is required: {value!r}"
            )
        return parsed.astimezone(UTC)

    raise ValueError(f"Unsupported timestamp type: {type(value).__name__}")


def normalize_severity(value: str | None = None) -> str:
    """Normalize severity to canonical uppercase set, defaulting None to 'INFO'."""
    if value is None:
        return "INFO"
    if not isinstance(value, str):
        raise ValueError("severity must be a string")

    cleaned = value.strip().upper()
    canonical = SEVERITY_ALIASES.get(cleaned, cleaned)
    if canonical not in CANONICAL_SEVERITIES:
        raise ValueError(f"Unknown severity value: {value!r}")
    return canonical


def normalize_event_type(value: str | None = None) -> str:
    """Normalize event_type to lowercase (<= 64 chars), defaulting None to 'log'."""
    if value is None:
        return "log"
    if not isinstance(value, str):
        raise ValueError("event_type must be a string")

    cleaned = value.strip().lower()
    if not cleaned:
        raise ValueError("event_type cannot be empty")
    if len(cleaned) > _MAX_EVENT_TYPE_LENGTH:
        raise ValueError(
            f"event_type exceeds maximum length of {_MAX_EVENT_TYPE_LENGTH} characters"
        )
    return cleaned


def compute_dedup_hash(
    service_name: str,
    timestamp_utc: datetime,
    event_type: str,
    severity: str,
    message: str,
    external_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> str:
    """Compute a deterministic 64-char lowercase SHA-256 deduplication hash."""
    cleaned_external_id = external_id.strip() if isinstance(external_id, str) else ""
    if cleaned_external_id:
        canonical = f"ext|{service_name}|{cleaned_external_id}"
    else:
        utc_ts = normalize_timestamp_utc(timestamp_utc)
        canonical = (
            f"fp|{service_name}|{utc_ts.isoformat()}|{event_type}|{severity}"
            f"|{message}|{trace_id or ''}|{span_id or ''}"
        )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
