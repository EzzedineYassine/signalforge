"""anomalies persistence table

Revision ID: 0003_anomalies
Revises: 0002_telemetry_signals
Create Date: 2026-11-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_anomalies"
down_revision: str | None = "0002_telemetry_signals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TSTZ = postgresql.TIMESTAMP(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
DOUBLE = postgresql.DOUBLE_PRECISION()


def upgrade() -> None:
    op.create_table(
        "anomalies",
        sa.Column("id", UUID, nullable=False),
        sa.Column("service_id", UUID, nullable=False),
        sa.Column("detector_name", sa.String(64), nullable=False),
        sa.Column("timestamp_utc", TSTZ, nullable=False),
        sa.Column("score", DOUBLE, nullable=False),
        sa.Column("threshold", DOUBLE, nullable=False),
        sa.Column("is_anomaly", sa.Boolean(), nullable=False),
        sa.Column("metric_names", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("details", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("dedup_hash", sa.String(64), nullable=False),
        sa.Column("detected_at", TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name="fk_anomalies_service_id_services",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_anomalies"),
        sa.UniqueConstraint("dedup_hash", name="uq_anomalies_dedup_hash"),
    )
    op.create_index(
        "ix_anomalies_service_id_timestamp_utc",
        "anomalies",
        ["service_id", sa.text("timestamp_utc DESC")],
    )
    op.create_index(
        "ix_anomalies_detector_name_is_anomaly_timestamp_utc",
        "anomalies",
        ["detector_name", "is_anomaly", sa.text("timestamp_utc DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_anomalies_detector_name_is_anomaly_timestamp_utc", table_name="anomalies")
    op.drop_index("ix_anomalies_service_id_timestamp_utc", table_name="anomalies")
    op.drop_table("anomalies")
