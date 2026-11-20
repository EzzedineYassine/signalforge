from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from apps.api.src.core import database
from apps.api.src.core.config import get_settings
from apps.api.src.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    test_engine = create_engine(
        get_settings().TEST_DATABASE_URL, pool_pre_ping=True, connect_args={"connect_timeout": 5}
    )
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "SessionLocal", database.sessionmaker(bind=test_engine))
    with TestClient(app) as c:
        yield c
    test_engine.dispose()