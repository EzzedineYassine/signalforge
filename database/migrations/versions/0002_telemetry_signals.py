"""telemetry signals (metric_points and trace_spans)

Revision ID: 0002_telemetry_signals
Revises: 0001_initial_schema
Create Date: 2026-11-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_telemetry_signals"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TSTZ = postgresql.TIMESTAMP(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
DOUBLE = postgresql.DOUBLE_PRECISION()


def upgrade() -> None:
    op.create_table(
        "metric_points",
        sa.Column("id", UUID, nullable=False),
        sa.Column("service_id", UUID, nullable=False),
        sa.Column("metric_name", sa.String(128), nullable=False),
        sa.Column("timestamp_utc", TSTZ, nullable=False),
        sa.Column("value", DOUBLE, nullable=False),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("labels", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("dedup_hash", sa.String(64), nullable=False),
        sa.Column("ingested_at", TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name="fk_metric_points_service_id_services", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_metric_points"),
        sa.UniqueConstraint("dedup_hash", name="uq_metric_points_dedup_hash"),
    )
    op.create_index(
        "ix_metric_points_service_id_metric_name_timestamp_utc",
        "metric_points",
        ["service_id", "metric_name", sa.text("timestamp_utc DESC")],
    )
    op.create_index("ix_metric_points_timestamp_utc", "metric_points", [sa.text("timestamp_utc DESC")])

    op.create_table(
        "trace_spans",
        sa.Column("id", UUID, nullable=False),
        sa.Column("service_id", UUID, nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("span_id", sa.String(64), nullable=False),
        sa.Column("parent_span_id", sa.String(64), nullable=True),
        sa.Column("operation_name", sa.String(256), nullable=False),
        sa.Column("span_kind", sa.String(32), server_default=sa.text("'INTERNAL'"), nullable=False),
        sa.Column("status_code", sa.String(32), server_default=sa.text("'UNSET'"), nullable=False),
        sa.Column("start_time_utc", TSTZ, nullable=False),
        sa.Column("end_time_utc", TSTZ, nullable=False),
        sa.Column("duration_ms", DOUBLE, nullable=False),
        sa.Column("attributes", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("ingested_at", TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "end_time_utc >= start_time_utc",
            name=op.f("ck_trace_spans_end_time_gte_start_time"),
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name=op.f("ck_trace_spans_duration_ms_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name="fk_trace_spans_service_id_services", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trace_spans"),
        sa.UniqueConstraint("trace_id", "span_id", name="uq_trace_spans_trace_id_span_id"),
    )
    op.create_index(
        "ix_trace_spans_service_id_start_time_utc",
        "trace_spans",
        ["service_id", sa.text("start_time_utc DESC")],
    )
    op.create_index("ix_trace_spans_trace_id", "trace_spans", ["trace_id"])
    op.create_index("ix_trace_spans_parent_span_id", "trace_spans", ["parent_span_id"])


def downgrade() -> None:
    op.drop_index("ix_trace_spans_parent_span_id", table_name="trace_spans")
    op.drop_index("ix_trace_spans_trace_id", table_name="trace_spans")
    op.drop_index("ix_trace_spans_service_id_start_time_utc", table_name="trace_spans")
    op.drop_table("trace_spans")

    op.drop_index("ix_metric_points_timestamp_utc", table_name="metric_points")
    op.drop_index("ix_metric_points_service_id_metric_name_timestamp_utc", table_name="metric_points")
    op.drop_table("metric_points")
