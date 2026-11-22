"""investigations persistence table

Revision ID: 0004_investigations
Revises: 0003_anomalies
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_investigations"
down_revision: str | None = "0003_anomalies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TSTZ = postgresql.TIMESTAMP(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
DOUBLE = postgresql.DOUBLE_PRECISION()


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("service_id", UUID, nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("window_start", TSTZ, nullable=False),
        sa.Column("window_end", TSTZ, nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("abstained", sa.Boolean(), nullable=False),
        sa.Column("report_json", JSONB, nullable=False),
        sa.Column("executed_nodes", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("duration_ms", DOUBLE, nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name="fk_investigations_service_id_services",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_investigations"),
    )
    op.create_index(
        "ix_investigations_service_id_created_at",
        "investigations",
        ["service_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_investigations_service_id_created_at", table_name="investigations")
    op.drop_table("investigations")
