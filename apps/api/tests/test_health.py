from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from apps.api.src.core import database


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


def test_health_degraded(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(database, "check_db_connection", lambda *a, **k: False)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "disconnected"}