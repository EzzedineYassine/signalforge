import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from apps.api.src.core import database
from apps.api.src.core.config import get_settings
from apps.api.src.main import app
from apps.api.src.models import Base

ROOT = Path(__file__).resolve().parents[3]
TEST_URL = get_settings().TEST_DATABASE_URL

# Alembic (env.py) must always target the test database during tests.
os.environ["ALEMBIC_DATABASE_URL"] = TEST_URL


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "database" / "migrations"))
    return cfg


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    engine = create_engine(TEST_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    command.upgrade(alembic_config(), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(test_engine: Engine) -> Iterator[Session]:
    with Session(test_engine) as session:
        yield session
    # Clean data between tests (schema is managed by Alembic only).
    tables = ", ".join(f'"{t}"' for t in Base.metadata.tables)
    with test_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def client(test_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "SessionLocal", database.sessionmaker(bind=test_engine))
    with TestClient(app) as c:
        yield c