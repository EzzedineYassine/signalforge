import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.src.models import (
    Base,
    Deployment,
    Event,
    Incident,
    IngestionFailure,
    MetricPoint,
    Service,
    TraceSpan,
)
from apps.api.tests.conftest import alembic_config

M1_TABLES = {"services", "events", "incidents", "deployments", "ingestion_failures"}
M2_TABLES = {"metric_points", "trace_spans"}
ALL_TABLES = M1_TABLES | M2_TABLES


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


def _metric_point(service_id: uuid.UUID, dedup: str = "m1") -> MetricPoint:
    return MetricPoint(
        service_id=service_id,
        metric_name="http.server.duration",
        timestamp_utc=datetime(2026, 1, 1, tzinfo=UTC),
        value=42.123456789,
        unit="ms",
        labels={"method": "POST", "status": 200},
        dedup_hash=dedup,
    )


def _trace_span(
    service_id: uuid.UUID,
    trace_id: str = "trace-1",
    span_id: str = "span-1",
    start: datetime | None = None,
    end: datetime | None = None,
    duration_ms: float = 12.5,
) -> TraceSpan:
    start_ts = start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    end_ts = end or datetime(2026, 1, 1, 12, 0, 0, 12_500, tzinfo=UTC)
    return TraceSpan(
        service_id=service_id,
        trace_id=trace_id,
        span_id=span_id,
        operation_name="POST /api/v1/checkout",
        start_time_utc=start_ts,
        end_time_utc=end_ts,
        duration_ms=duration_ms,
        attributes={"http.status_code": 200},
    )


def test_tables_and_indexes_exist(test_engine: Engine) -> None:
    assert ALL_TABLES == set(Base.metadata.tables)
    insp = inspect(test_engine)
    assert ALL_TABLES <= set(insp.get_table_names())

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
    assert {
        "ix_metric_points_service_id_metric_name_timestamp_utc",
        "ix_metric_points_timestamp_utc",
    } <= {i["name"] for i in insp.get_indexes("metric_points")}
    assert {
        "ix_trace_spans_service_id_start_time_utc",
        "ix_trace_spans_trace_id",
        "ix_trace_spans_parent_span_id",
    } <= {i["name"] for i in insp.get_indexes("trace_spans")}

    assert "uq_services_name" in {u["name"] for u in insp.get_unique_constraints("services")}
    assert "uq_events_dedup_hash" in {u["name"] for u in insp.get_unique_constraints("events")}
    assert "uq_metric_points_dedup_hash" in {
        u["name"] for u in insp.get_unique_constraints("metric_points")
    }
    assert "uq_trace_spans_trace_id_span_id" in {
        u["name"] for u in insp.get_unique_constraints("trace_spans")
    }

    assert "fk_events_service_id_services" in {
        f["name"] for f in insp.get_foreign_keys("events")
    }
    assert "fk_metric_points_service_id_services" in {
        f["name"] for f in insp.get_foreign_keys("metric_points")
    }
    assert "fk_trace_spans_service_id_services" in {
        f["name"] for f in insp.get_foreign_keys("trace_spans")
    }

    trace_checks = {c["name"] for c in insp.get_check_constraints("trace_spans")}
    assert {
        "ck_trace_spans_end_time_gte_start_time",
        "ck_trace_spans_duration_ms_non_negative",
    } <= trace_checks


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
    mp = _metric_point(svc.id)
    mp.timestamp_utc = local
    span = _trace_span(
        svc.id,
        start=local,
        end=local + timedelta(milliseconds=125),
        duration_ms=125.75,
    )
    db_session.add_all([ev, inc, dep, fail, mp, span])
    db_session.commit()
    db_session.expire_all()

    got_ev = db_session.get(Event, ev.id)
    assert got_ev is not None
    assert got_ev.timestamp_utc == local
    assert got_ev.timestamp_utc.utcoffset() is not None
    assert got_ev.attributes == {"k": [1, 2], "nested": {"a": "b"}}

    got_svc = db_session.get(Service, svc.id)
    assert got_svc is not None
    assert got_svc.environment == "default"
    assert len(got_svc.metric_points) == 1
    assert len(got_svc.trace_spans) == 1

    assert db_session.get(Incident, inc.id).status == "open"
    assert db_session.get(Deployment, dep.id).metadata_json == {"x": 1}
    assert db_session.get(IngestionFailure, fail.id).raw_payload == {"a": 1}

    got_mp = db_session.get(MetricPoint, mp.id)
    assert got_mp is not None
    assert got_mp.timestamp_utc == local
    assert got_mp.value == pytest.approx(42.123456789, rel=1e-12)
    assert got_mp.unit == "ms"
    assert got_mp.labels == {"method": "POST", "status": 200}
    assert got_mp.service.id == svc.id

    got_span = db_session.get(TraceSpan, span.id)
    assert got_span is not None
    assert got_span.start_time_utc == local
    assert got_span.end_time_utc == local + timedelta(milliseconds=125)
    assert got_span.duration_ms == pytest.approx(125.75)
    assert got_span.span_kind == "INTERNAL"
    assert got_span.status_code == "UNSET"
    assert got_span.attributes == {"http.status_code": 200}
    assert got_span.service.id == svc.id


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


def test_duplicate_metric_point_dedup_hash(db_session: Session) -> None:
    svc = _service()
    db_session.add(svc)
    db_session.flush()
    db_session.add(_metric_point(svc.id, "same-metric"))
    db_session.commit()
    db_session.add(_metric_point(svc.id, "same-metric"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_metric_point_requires_existing_service(db_session: Session) -> None:
    db_session.add(_metric_point(uuid.uuid4()))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_duplicate_trace_span_pair(db_session: Session) -> None:
    svc = _service()
    db_session.add(svc)
    db_session.flush()
    db_session.add(_trace_span(svc.id, trace_id="t-dup", span_id="s-dup"))
    db_session.commit()
    db_session.add(_trace_span(svc.id, trace_id="t-dup", span_id="s-dup"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_trace_span_requires_existing_service(db_session: Session) -> None:
    db_session.add(_trace_span(uuid.uuid4()))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_trace_span_end_time_before_start_time_rejected(db_session: Session) -> None:
    svc = _service()
    db_session.add(svc)
    db_session.flush()
    start = datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)
    end = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
    db_session.add(_trace_span(svc.id, start=start, end=end, duration_ms=0.0))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_trace_span_negative_duration_rejected(db_session: Session) -> None:
    svc = _service()
    db_session.add(svc)
    db_session.flush()
    db_session.add(_trace_span(svc.id, duration_ms=-1.0))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_migration_0002_downgrade_and_upgrade(test_engine: Engine) -> None:
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0001_initial_schema")
        existing = set(inspect(test_engine).get_table_names())
        assert M1_TABLES <= existing
        assert M2_TABLES.isdisjoint(existing)
    finally:
        command.upgrade(cfg, "head")
    assert ALL_TABLES <= set(inspect(test_engine).get_table_names())


def test_migration_roundtrip(test_engine: Engine) -> None:
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "base")
        remaining = ALL_TABLES & set(inspect(test_engine).get_table_names())
        assert remaining == set()
    finally:
        command.upgrade(cfg, "head")
    assert ALL_TABLES <= set(inspect(test_engine).get_table_names())
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
