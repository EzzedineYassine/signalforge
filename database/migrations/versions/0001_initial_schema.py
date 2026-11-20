"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TSTZ = postgresql.TIMESTAMP(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("environment", sa.String(64), server_default=sa.text("'default'"), nullable=False),
        sa.Column("owner_team", sa.String(128), nullable=True),
        sa.Column("is_auto_discovered", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_services"),
        sa.UniqueConstraint("name", name="uq_services_name"),
    )
    op.create_table(
        "events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("service_id", UUID, nullable=False),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("dedup_hash", sa.String(64), nullable=False),
        sa.Column("timestamp_utc", TSTZ, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("span_id", sa.String(64), nullable=True),
        sa.Column("attributes", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("ingested_at", TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name="fk_events_service_id_services", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("dedup_hash", name="uq_events_dedup_hash"),
    )
    op.create_index(
        "ix_events_service_id_timestamp_utc", "events", ["service_id", sa.text("timestamp_utc DESC")]
    )
    op.create_index("ix_events_timestamp_utc", "events", [sa.text("timestamp_utc DESC")])
    op.create_index("ix_events_trace_id", "events", ["trace_id"])
    op.create_table(
        "incidents",
        sa.Column("id", UUID, nullable=False),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("service_id", UUID, nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), server_default=sa.text("'open'"), nullable=False),
        sa.Column("started_at", TSTZ, nullable=False),
        sa.Column("resolved_at", TSTZ, nullable=True),
        sa.Column("created_at", TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name="fk_incidents_service_id_services", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_incidents"),
        sa.UniqueConstraint("external_id", name="uq_incidents_external_id"),
    )
    op.create_index(
        "ix_incidents_service_id_started_at", "incidents", ["service_id", sa.text("started_at DESC")]
    )
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_table(
        "deployments",
        sa.Column("id", UUID, nullable=False),
        sa.Column("service_id", UUID, nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("deployed_by", sa.String(128), nullable=True),
        sa.Column("deployed_at", TSTZ, nullable=False),
        sa.Column("metadata_json", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name="fk_deployments_service_id_services", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deployments"),
    )
    op.create_index(
        "ix_deployments_service_id_deployed_at",
        "deployments",
        ["service_id", sa.text("deployed_at DESC")],
    )
    op.create_table(
        "ingestion_failures",
        sa.Column("id", UUID, nullable=False),
        sa.Column("batch_id", UUID, nullable=False),
        sa.Column("record_index", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=False),
        sa.Column("raw_payload", JSONB, nullable=False),
        sa.Column("failed_at", TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_failures"),
    )
    op.create_index("ix_ingestion_failures_batch_id", "ingestion_failures", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_ingestion_failures_batch_id", table_name="ingestion_failures")
    op.drop_table("ingestion_failures")
    op.drop_index("ix_deployments_service_id_deployed_at", table_name="deployments")
    op.drop_table("deployments")
    op.drop_index("ix_incidents_status", table_name="incidents")
    op.drop_index("ix_incidents_service_id_started_at", table_name="incidents")
    op.drop_table("incidents")
    op.drop_index("ix_events_trace_id", table_name="events")
    op.drop_index("ix_events_timestamp_utc", table_name="events")
    op.drop_index("ix_events_service_id_timestamp_utc", table_name="events")
    op.drop_table("events")
    op.drop_table("services")