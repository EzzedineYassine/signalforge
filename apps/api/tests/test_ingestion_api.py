import ast
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from apps.api.src.models import Event, IngestionFailure, Service

INGEST_URL = "/api/v1/events/ingest"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _sample_events() -> list[dict[str, object]]:
    return [
        {
            "service": "Payment_Service ",
            "timestamp": "2026-11-20T15:30:00+01:00",
            "message": "Gateway timeout on charge",
            "event_type": "HTTP_ERROR",
            "severity": "err",
            "external_id": "pay-evt-1",
            "trace_id": "trace-1",
            "span_id": "span-1",
            "attributes": {"status_code": 504, "attempt": 1},
        },
        {
            "service": "payment-service",
            "timestamp": "2026-11-20T14:31:00Z",
            "message": "Retry succeeded",
            "severity": "INFO",
            "attributes": {"attempt": 2},
        },
        {
            "service": "auth.api",
            "timestamp": 1_700_000_000_123,
            "message": "JWT signing key rotated",
            "severity": "warn",
        },
    ]


def test_ingest_valid_batch_creates_services_and_utc_events(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(INGEST_URL, json={"events": _sample_events()})

    assert response.status_code == 200
    body = response.json()
    assert UUID(body["batch_id"])
    assert body["received"] == 3
    assert body["accepted"] == 3
    assert body["duplicates"] == 0
    assert body["rejected"] == 0
    assert body["errors"] == []

    services = db_session.scalars(select(Service).order_by(Service.name)).all()
    assert [(s.name, s.is_auto_discovered, s.environment) for s in services] == [
        ("auth-api", True, "default"),
        ("payment-service", True, "default"),
    ]

    events = db_session.scalars(select(Event).order_by(Event.timestamp_utc)).all()
    assert len(events) == 3

    by_msg = {e.message: e for e in events}
    charge_evt = by_msg["Gateway timeout on charge"]
    assert charge_evt.timestamp_utc == datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)
    assert charge_evt.timestamp_utc.utcoffset() == UTC.utcoffset(None)
    assert charge_evt.event_type == "http_error"
    assert charge_evt.severity == "ERROR"
    assert charge_evt.external_id == "pay-evt-1"
    assert charge_evt.trace_id == "trace-1"
    assert charge_evt.span_id == "span-1"
    assert charge_evt.attributes == {"status_code": 504, "attempt": 1}

    jwt_evt = by_msg["JWT signing key rotated"]
    assert jwt_evt.timestamp_utc == datetime(2023, 11, 14, 22, 13, 20, 123_000, tzinfo=UTC)
    assert jwt_evt.severity == "WARNING"
    assert jwt_evt.event_type == "log"


def test_ingest_cross_batch_idempotency(client: TestClient, db_session: Session) -> None:
    payload = {"events": _sample_events()}

    first = client.post(INGEST_URL, json=payload)
    assert first.status_code == 200
    assert first.json()["accepted"] == 3
    assert first.json()["duplicates"] == 0

    second = client.post(INGEST_URL, json=payload)
    assert second.status_code == 200
    body = second.json()
    assert body["received"] == 3
    assert body["accepted"] == 0
    assert body["duplicates"] == 3
    assert body["rejected"] == 0
    assert body["errors"] == []

    assert db_session.scalar(select(func.count(Event.id))) == 3
    assert db_session.scalar(select(func.count(Service.id))) == 2


def test_ingest_in_batch_duplicate(client: TestClient, db_session: Session) -> None:
    event = {
        "service": "billing-api",
        "timestamp": "2026-11-20T14:30:00Z",
        "message": "Invoice created",
        "severity": "INFO",
    }
    response = client.post(INGEST_URL, json={"events": [event, event]})

    assert response.status_code == 200
    body = response.json()
    assert body["received"] == 2
    assert body["accepted"] == 1
    assert body["duplicates"] == 1
    assert body["rejected"] == 0
    assert body["errors"] == []

    assert db_session.scalar(select(func.count(Event.id))) == 1


def test_ingest_equivalent_timezone_deduplication(
    client: TestClient, db_session: Session
) -> None:
    events = [
        {
            "service": "Payment_Service",
            "timestamp": "2026-11-20T15:30:00+01:00",
            "message": "Downstream latency spike",
            "severity": "warn",
        },
        {
            "service": "payment-service",
            "timestamp": "2026-11-20T14:30:00Z",
            "message": "Downstream latency spike",
            "severity": "WARNING",
        },
    ]

    response = client.post(INGEST_URL, json={"events": events})
    assert response.status_code == 200
    body = response.json()
    assert body["received"] == 2
    assert body["accepted"] == 1
    assert body["duplicates"] == 1
    assert body["rejected"] == 0

    # Submitting the Z representation again in a subsequent batch is also a duplicate.
    follow_up = client.post(INGEST_URL, json={"events": [events[1]]})
    assert follow_up.status_code == 200
    assert follow_up.json()["accepted"] == 0
    assert follow_up.json()["duplicates"] == 1

    assert db_session.scalar(select(func.count(Event.id))) == 1


def test_ingest_mixed_batch_persists_valid_and_failures(
    client: TestClient, db_session: Session
) -> None:
    mixed = [
        # 0: valid
        {
            "service": "orders.api",
            "timestamp": "2026-11-20T14:30:00Z",
            "message": "Order placed",
        },
        # 1: invalid (naive timestamp)
        {
            "service": "orders.api",
            "timestamp": "2026-11-20T14:30:00",
            "message": "Naive timestamp event",
        },
        # 2: invalid (missing message)
        {
            "service": "orders.api",
            "timestamp": "2026-11-20T14:32:00Z",
        },
        # 3: valid
        {
            "service": "inventory.api",
            "timestamp": "2026-11-20T14:33:00Z",
            "message": "Stock reserved",
        },
    ]

    response = client.post(INGEST_URL, json={"events": mixed})
    assert response.status_code == 200
    body = response.json()
    batch_id = UUID(body["batch_id"])

    assert body["received"] == 4
    assert body["accepted"] == 2
    assert body["duplicates"] == 0
    assert body["rejected"] == 2
    assert [err["index"] for err in body["errors"]] == [1, 2]
    assert all(err["error_code"] == "VALIDATION_ERROR" for err in body["errors"])

    assert db_session.scalar(select(func.count(Event.id))) == 2

    failures = db_session.scalars(
        select(IngestionFailure)
        .where(IngestionFailure.batch_id == batch_id)
        .order_by(IngestionFailure.record_index)
    ).all()
    assert len(failures) == 2
    assert [f.record_index for f in failures] == [1, 2]
    assert failures[0].raw_payload == mixed[1]
    assert failures[1].raw_payload == mixed[2]


def test_ingest_all_invalid_batch_with_non_dict_record(
    client: TestClient, db_session: Session
) -> None:
    response = client.post(
        INGEST_URL,
        json={"events": ["not-an-object", {"service": "$$$", "timestamp": "bad", "message": ""}]},
    )

    assert response.status_code == 200
    body = response.json()
    batch_id = UUID(body["batch_id"])
    assert body["received"] == 2
    assert body["accepted"] == 0
    assert body["duplicates"] == 0
    assert body["rejected"] == 2
    assert [e["error_code"] for e in body["errors"]] == ["INVALID_RECORD_TYPE", "VALIDATION_ERROR"]

    assert db_session.scalar(select(func.count(Service.id))) == 0
    assert db_session.scalar(select(func.count(Event.id))) == 0

    failures = db_session.scalars(
        select(IngestionFailure)
        .where(IngestionFailure.batch_id == batch_id)
        .order_by(IngestionFailure.record_index)
    ).all()
    assert len(failures) == 2
    assert failures[0].raw_payload == {"raw": "'not-an-object'"}


def test_ingest_preserves_preexisting_service_metadata(
    client: TestClient, db_session: Session
) -> None:
    manual = Service(
        name="checkout-api",
        environment="production",
        owner_team="checkout-team",
        is_auto_discovered=False,
    )
    db_session.add(manual)
    db_session.commit()

    response = client.post(
        INGEST_URL,
        json={
            "events": [
                {
                    "service": "Checkout_API",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "Checkout started",
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1

    db_session.expire_all()
    refreshed = db_session.scalar(select(Service).where(Service.name == "checkout-api"))
    assert refreshed is not None
    assert refreshed.id == manual.id
    assert refreshed.environment == "production"
    assert refreshed.owner_team == "checkout-team"
    assert refreshed.is_auto_discovered is False


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"events": []},
        {},
        {"events": "not-a-list"},
        {"events": {"service": "api"}},
        {"events": 123},
        {"events": [{"service": "a", "timestamp": "2026-11-20T14:30:00Z", "message": "m"}] * 1001},
    ],
)
def test_ingest_invalid_envelope_returns_422(
    client: TestClient, db_session: Session, bad_payload: dict[str, object]
) -> None:
    response = client.post(INGEST_URL, json=bad_payload)
    assert response.status_code == 422


def test_ingest_db_error_rolls_back_and_returns_500(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollback_called = False
    original_rollback = Session.rollback

    def spy_rollback(self: Session) -> None:
        nonlocal rollback_called
        rollback_called = True
        original_rollback(self)

    def fail_commit(self: Session) -> None:
        raise OperationalError("COMMIT", {}, Exception("simulated DB failure"))

    monkeypatch.setattr(Session, "rollback", spy_rollback)
    monkeypatch.setattr(Session, "commit", fail_commit)

    response = client.post(
        INGEST_URL,
        json={
            "events": [
                {
                    "service": "api",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "message": "should roll back",
                }
            ]
        },
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Database error while ingesting event batch"}
    assert rollback_called is True
    assert db_session.scalar(select(func.count(Event.id))) == 0


def test_pipeline_has_no_fastapi_imports() -> None:
    path = REPO_ROOT / "services" / "ingestion" / "pipeline.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    assert imported_roots.isdisjoint({"fastapi", "starlette"})
