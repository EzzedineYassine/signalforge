import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.src.models.base import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _tstz(**kw: Any):
    return mapped_column(TIMESTAMP(timezone=True), **kw)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    environment: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'default'")
    )
    owner_team: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_auto_discovered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = _tstz(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    timestamp_utc: Mapped[datetime] = _tstz(nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    ingested_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_events_service_id_timestamp_utc", "service_id", text("timestamp_utc DESC")),
        Index("ix_events_timestamp_utc", text("timestamp_utc DESC")),
        Index("ix_events_trace_id", "trace_id"),
    )


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    external_id: Mapped[str | None] = mapped_column(String(256), unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'open'")
    )
    started_at: Mapped[datetime] = _tstz(nullable=False)
    resolved_at: Mapped[datetime | None] = _tstz(nullable=True)
    created_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_incidents_service_id_started_at", "service_id", text("started_at DESC")),
        Index("ix_incidents_status", "status"),
    )


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    deployed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deployed_at: Mapped[datetime] = _tstz(nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index("ix_deployments_service_id_deployed_at", "service_id", text("deployed_at DESC")),
    )


class IngestionFailure(Base):
    __tablename__ = "ingestion_failures"

    id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    record_index: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    error_detail: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    failed_at: Mapped[datetime] = _tstz(nullable=False, server_default=func.now())