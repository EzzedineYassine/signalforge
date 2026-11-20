import os

from alembic import context
from sqlalchemy import create_engine, pool

from apps.api.src.core.config import get_settings
from apps.api.src.models import Base

config = context.config
target_metadata = Base.metadata


def get_url() -> str:
    return os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().DATABASE_URL


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()