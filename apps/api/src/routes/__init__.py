from datetime import datetime

from fastapi import HTTPException, status

from services.ingestion.normalizer import (
    normalize_event_type,
    normalize_service_name,
    normalize_severity,
    normalize_timestamp_utc,
)


def parse_service_filter(service: str | None) -> str | None:
    if service is None:
        return None
    try:
        return normalize_service_name(service)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid service filter: {exc}",
        ) from exc


def parse_severity_filter(severity: str | None) -> str | None:
    if severity is None:
        return None
    try:
        return normalize_severity(severity)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid severity filter: {exc}",
        ) from exc


def parse_event_type_filter(event_type: str | None) -> str | None:
    if event_type is None:
        return None
    try:
        return normalize_event_type(event_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid event_type filter: {exc}",
        ) from exc


def parse_time_range(
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    start_utc: datetime | None = None
    end_utc: datetime | None = None

    if start_time is not None:
        try:
            start_utc = normalize_timestamp_utc(start_time)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid start_time: {exc}",
            ) from exc

    if end_time is not None:
        try:
            end_utc = normalize_timestamp_utc(end_time)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid end_time: {exc}",
            ) from exc

    if start_utc is not None and end_utc is not None and start_utc > end_utc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_time must be less than or equal to end_time",
        )

    return start_utc, end_utc
