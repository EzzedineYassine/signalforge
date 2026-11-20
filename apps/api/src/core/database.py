import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from apps.api.src.core.config import get_settings

logger = logging.getLogger(__name__)

engine = create_engine(
    get_settings().DATABASE_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_db_connection(bind=None) -> bool:
    """Run SELECT 1; return False on failure (logs only the exception type)."""
    target = bind if bind is not None else engine
    try:
        with target.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError as exc:
        logger.error("Database connectivity check failed: %s", type(exc).__name__)
        return False