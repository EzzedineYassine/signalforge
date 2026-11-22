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


def test_cors_allows_configured_frontend_origin_and_blocks_unknown(client: TestClient) -> None:
    allowed = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"

    preflight = client.options(
        "/api/v1/services",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers.get("access-control-allow-origin") == "http://localhost:5173"

    blocked = client.get("/health", headers={"Origin": "http://untrusted.example"})
    assert blocked.status_code == 200
    assert blocked.headers.get("access-control-allow-origin") is None
