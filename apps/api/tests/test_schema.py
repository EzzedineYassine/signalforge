import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.src.models import Deployment, Event, Incident, IngestionFailure, Service
from apps.api.tests.conftest import alembic_config

TABLES = {"services", "events", "incidents", "deployments", "ingestion_failures"}


def _service(name: str = "svc-a") -> Service:
    return Service(name=name)


def _event(service_id: uuid.UUID, dedup: str = "h1") -> Event:
    return Event(
        service_id=service_id,
        dedup_hash=dedup,
        timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
        event_type="log",
        severity="error",
        message="boom",
    )


def test_tables_and_indexes_exist(test_engine: Engine) -> None:
    insp = inspect(test_engine)
    assert TABLES <= set(insp.get_table_names())
    ev = {i["name"] for i in insp.get_indexes("events")}
    assert {
        "ix_events_service_id_timestamp_utc",
        "ix_events_timestamp_utc",
        "ix_events_trace_id",
    } <= ev
    assert {"ix_incidents_service_id_started_at", "ix_incidents_status"} <= {
        i["name"] for i in insp.get_indexes("incidents")
    }
    assert "ix_deployments_service_id_deployed_at" in {
        i["name"] for i in insp.get_indexes("deployments")
    }
    assert "ix_ingestion_failures_batch_id" in {
        i["name"] for i in insp.get_indexes("ingestion_failures")
    }
    uqs = {u["name"] for u in insp.get_unique_constraints("services")}
    assert "uq_services_name" in uqs
    assert "uq_events_dedup_hash" in {u["name"] for u in insp.get_unique_constraints("events")}
    fks = {f["name"] for f in insp.get_foreign_keys("events")}
    assert "fk_events_service_id_services" in fks


def test_insert_all_models_roundtrip(db_session: Session) -> None:
    svc = _service()
    db_session.add(svc)
    db_session.flush()
    # Non-UTC input must come back as the same instant.
    local = datetime(2026, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    ev = _event(svc.id)
    ev.timestamp_utc = local
    ev.attributes = {"k": [1, 2], "nested": {"a": "b"}}
    inc = Incident(title="t", service_id=svc.id, severity="high", started_at=local)
    dep = Deployment(
        service_id=svc.id,
        version="1.0",
        status="success",
        deployed_at=local,
        metadata_json={"x": 1},
    )
    batch = uuid.uuid4()
    fail = IngestionFailure(
        batch_id=batch, record_index=3, error_code="bad", error_detail="d", raw_payload={"a": 1}
    )
    db_session.add_all([ev, inc, dep, fail])
    db_session.commit()
    db_session.expire_all()

    got = db_session.get(Event, ev.id)
    assert got is not None
    assert got.timestamp_utc == local
    assert got.timestamp_utc.utcoffset() is not None
    assert got.attributes == {"k": [1, 2], "nested": {"a": "b"}}
    assert db_session.get(Service, svc.id).environment == "default"
    assert db_session.get(Incident, inc.id).status == "open"
    assert db_session.get(Deployment, dep.id).metadata_json == {"x": 1}
    assert db_session.get(IngestionFailure, fail.id).raw_payload == {"a": 1}


def test_duplicate_service_name(db_session: Session) -> None:
    db_session.add(_service("dup"))
    db_session.commit()
    db_session.add(_service("dup"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_duplicate_dedup_hash(db_session: Session) -> None:
    svc = _service()
    db_session.add(svc)
    db_session.flush()
    db_session.add(_event(svc.id, "same"))
    db_session.commit()
    db_session.add(_event(svc.id, "same"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_event_requires_existing_service(db_session: Session) -> None:
    db_session.add(_event(uuid.uuid4()))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_migration_roundtrip(test_engine: Engine) -> None:
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "base")
        remaining = TABLES & set(inspect(test_engine).get_table_names())
        assert remaining == set()
    finally:
        command.upgrade(cfg, "head")
    assert TABLES <= set(inspect(test_engine).get_table_names())
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1