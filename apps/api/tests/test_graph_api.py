import math
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.src.models import Anomaly, Incident, Service
from apps.api.src.services.anomaly_service import compute_anomaly_dedup_hash
from services.graph import DEFAULT_CORRELATION_NOTE


def _seed_standard_topology_traces(client: TestClient) -> None:
    """Ingest two traces forming api-gateway -> checkout-service -> payment-service + self-call."""
    t1 = datetime(2026, 11, 20, 14, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 20, 14, 10, 0, tzinfo=UTC)

    payload = {
        "spans": [
            # Trace 1 (14:00Z): all OK + self-call inside payment-service
            {
                "service": "api-gateway",
                "trace_id": "trace-graph-1",
                "span_id": "span-gw-1",
                "parent_span_id": None,
                "operation_name": "POST /v1/checkout",
                "span_kind": "SERVER",
                "status_code": "OK",
                "start_time": t1.isoformat(),
                "end_time": (t1 + timedelta(milliseconds=120)).isoformat(),
                "duration_ms": 120.0,
            },
            {
                "service": "checkout-service",
                "trace_id": "trace-graph-1",
                "span_id": "span-co-1",
                "parent_span_id": "span-gw-1",
                "operation_name": "checkout.process",
                "span_kind": "SERVER",
                "status_code": "OK",
                "start_time": (t1 + timedelta(milliseconds=5)).isoformat(),
                "end_time": (t1 + timedelta(milliseconds=45)).isoformat(),
                "duration_ms": 40.0,
            },
            {
                "service": "payment-service",
                "trace_id": "trace-graph-1",
                "span_id": "span-pay-1",
                "parent_span_id": "span-co-1",
                "operation_name": "payment.authorize",
                "span_kind": "SERVER",
                "status_code": "OK",
                "start_time": (t1 + timedelta(milliseconds=10)).isoformat(),
                "end_time": (t1 + timedelta(milliseconds=30)).isoformat(),
                "duration_ms": 20.0,
            },
            # Self-call span inside payment-service (must NOT produce a self-loop edge)
            {
                "service": "payment-service",
                "trace_id": "trace-graph-1",
                "span_id": "span-pay-self-1",
                "parent_span_id": "span-pay-1",
                "operation_name": "payment.validate_card",
                "span_kind": "INTERNAL",
                "status_code": "OK",
                "start_time": (t1 + timedelta(milliseconds=12)).isoformat(),
                "end_time": (t1 + timedelta(milliseconds=17)).isoformat(),
                "duration_ms": 5.0,
            },
            # Trace 2 (14:10Z): payment-service returns ERROR
            {
                "service": "api-gateway",
                "trace_id": "trace-graph-2",
                "span_id": "span-gw-2",
                "parent_span_id": None,
                "operation_name": "POST /v1/checkout",
                "span_kind": "SERVER",
                "status_code": "ERROR",
                "start_time": t2.isoformat(),
                "end_time": (t2 + timedelta(milliseconds=200)).isoformat(),
                "duration_ms": 200.0,
            },
            {
                "service": "checkout-service",
                "trace_id": "trace-graph-2",
                "span_id": "span-co-2",
                "parent_span_id": "span-gw-2",
                "operation_name": "checkout.process",
                "span_kind": "SERVER",
                "status_code": "OK",
                "start_time": (t2 + timedelta(milliseconds=5)).isoformat(),
                "end_time": (t2 + timedelta(milliseconds=65)).isoformat(),
                "duration_ms": 60.0,
            },
            {
                "service": "payment-service",
                "trace_id": "trace-graph-2",
                "span_id": "span-pay-2",
                "parent_span_id": "span-co-2",
                "operation_name": "payment.authorize",
                "span_kind": "SERVER",
                "status_code": "ERROR",
                "start_time": (t2 + timedelta(milliseconds=10)).isoformat(),
                "end_time": (t2 + timedelta(milliseconds=110)).isoformat(),
                "duration_ms": 100.0,
            },
        ]
    }
    res = client.post("/api/v1/traces/ingest", json=payload)
    assert res.status_code == 200
    assert res.json()["accepted"] == 7
    assert res.json()["rejected"] == 0


def test_dependency_graph_empty_db(client: TestClient, db_session: Session) -> None:
    response = client.get("/api/v1/graph/dependencies")
    assert response.status_code == 200
    assert response.json() == {
        "node_count": 0,
        "edge_count": 0,
        "nodes": [],
        "edges": [],
    }


def test_dependency_graph_from_trace_spans_and_time_filter(
    client: TestClient,
    db_session: Session,
) -> None:
    _seed_standard_topology_traces(client)

    res = client.get("/api/v1/graph/dependencies")
    assert res.status_code == 200
    body = res.json()
    assert body["node_count"] == 3
    assert body["edge_count"] == 2
    assert body["nodes"] == ["api-gateway", "checkout-service", "payment-service"]

    edges_by_pair = {(e["caller"], e["callee"]): e for e in body["edges"]}
    assert set(edges_by_pair.keys()) == {
        ("api-gateway", "checkout-service"),
        ("checkout-service", "payment-service"),
    }
    # Self-loop excluded
    assert ("payment-service", "payment-service") not in edges_by_pair

    gw_to_co = edges_by_pair[("api-gateway", "checkout-service")]
    assert gw_to_co["call_count"] == 2
    assert gw_to_co["error_count"] == 0
    assert math.isclose(gw_to_co["error_rate"], 0.0, rel_tol=1e-9)
    assert math.isclose(gw_to_co["avg_duration_ms"], 50.0, rel_tol=1e-9)

    co_to_pay = edges_by_pair[("checkout-service", "payment-service")]
    assert co_to_pay["call_count"] == 2
    assert co_to_pay["error_count"] == 1
    assert math.isclose(co_to_pay["error_rate"], 0.5, rel_tol=1e-9)
    assert math.isclose(co_to_pay["avg_duration_ms"], 60.0, rel_tol=1e-9)

    # Time-window filter narrows edge aggregation to Trace 2 (14:10Z) only
    res_filtered = client.get(
        "/api/v1/graph/dependencies",
        params={
            "start_time": "2026-11-20T14:05:00Z",
            "end_time": "2026-11-20T14:15:00Z",
        },
    )
    assert res_filtered.status_code == 200
    filtered_edges = {
        (e["caller"], e["callee"]): e for e in res_filtered.json()["edges"]
    }
    assert filtered_edges[("checkout-service", "payment-service")]["call_count"] == 1
    assert filtered_edges[("checkout-service", "payment-service")]["error_count"] == 1
    assert math.isclose(
        filtered_edges[("checkout-service", "payment-service")]["avg_duration_ms"],
        100.0,
        rel_tol=1e-9,
    )


def test_service_impact_endpoint_and_404(client: TestClient, db_session: Session) -> None:
    _seed_standard_topology_traces(client)

    res = client.get("/api/v1/graph/services/checkout-service/impact")
    assert res.status_code == 200
    body = res.json()
    assert body["target_service"] == "checkout-service"
    assert body["max_depth"] == 3
    assert body["dependencies"] == [{"service": "payment-service", "hop_distance": 1}]
    assert body["affected_callers"] == [{"service": "api-gateway", "hop_distance": 1}]
    assert body["all_related_services"] == ["api-gateway", "payment-service"]

    # max_depth=1 cuts off 2-hop dependency payment-service from api-gateway
    res_d1 = client.get("/api/v1/graph/services/api-gateway/impact", params={"max_depth": 1})
    assert res_d1.status_code == 200
    assert res_d1.json()["dependencies"] == [{"service": "checkout-service", "hop_distance": 1}]

    res_d2 = client.get("/api/v1/graph/services/api-gateway/impact", params={"max_depth": 2})
    assert res_d2.status_code == 200
    assert res_d2.json()["dependencies"] == [
        {"service": "checkout-service", "hop_distance": 1},
        {"service": "payment-service", "hop_distance": 2},
    ]

    # Unknown service returns HTTP 404
    res_404 = client.get("/api/v1/graph/services/nonexistent-service/impact")
    assert res_404.status_code == 404


def test_correlation_timeline_end_to_end(client: TestClient, db_session: Session) -> None:
    _seed_standard_topology_traces(client)

    window_start = datetime(2026, 11, 20, 14, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)

    # 1. Seed deployments:
    #    - 30m before window_start on payment-service (INCLUDED)
    #    - 90m before window_start on payment-service (EXCLUDED, outside 60m lookback)
    #    - 15m before window_start on unrelated analytics-worker (EXCLUDED)
    dep_res = client.post(
        "/api/v1/deployments/ingest",
        json={
            "deployments": [
                {
                    "service": "payment-service",
                    "version": "v1.9.0",
                    "deployed_at": (window_start - timedelta(minutes=30)).isoformat(),
                    "status": "succeeded",
                    "commit_sha": "abc1234",
                },
                {
                    "service": "payment-service",
                    "version": "v1.8.0",
                    "deployed_at": (window_start - timedelta(minutes=90)).isoformat(),
                    "status": "succeeded",
                },
                {
                    "service": "analytics-worker",
                    "version": "v0.4.2",
                    "deployed_at": (window_start - timedelta(minutes=15)).isoformat(),
                    "status": "succeeded",
                },
            ]
        },
    )
    assert dep_res.status_code == 200
    assert dep_res.json()["accepted"] == 3

    # 2. Seed events:
    #    - ERROR on payment-service inside window (INCLUDED)
    #    - INFO on checkout-service inside window (EXCLUDED, below WARNING)
    #    - ERROR on unrelated analytics-worker inside window (EXCLUDED)
    evt_res = client.post(
        "/api/v1/events/ingest",
        json={
            "events": [
                {
                    "service": "payment-service",
                    "timestamp": (window_start + timedelta(minutes=8)).isoformat(),
                    "severity": "ERROR",
                    "message": "Upstream card processor 504 timeout",
                    "event_type": "log",
                    "trace_id": "trace-graph-2",
                },
                {
                    "service": "checkout-service",
                    "timestamp": (window_start + timedelta(minutes=7)).isoformat(),
                    "severity": "INFO",
                    "message": "Routine health probe succeeded",
                    "event_type": "log",
                },
                {
                    "service": "analytics-worker",
                    "timestamp": (window_start + timedelta(minutes=9)).isoformat(),
                    "severity": "ERROR",
                    "message": "Unrelated batch export failed",
                    "event_type": "log",
                },
            ]
        },
    )
    assert evt_res.status_code == 200
    assert evt_res.json()["accepted"] == 3

    # 3. Seed anomalies (1 positive anomaly + 1 non-anomaly) and 1 incident via ORM
    checkout_svc = db_session.scalar(select(Service).where(Service.name == "checkout-service"))
    assert checkout_svc is not None

    anom_ts = window_start + timedelta(minutes=5)
    normal_ts = window_start + timedelta(minutes=6)
    db_session.add_all(
        [
            Anomaly(
                service_id=checkout_svc.id,
                detector_name="lstm_autoencoder",
                timestamp_utc=anom_ts,
                score=9.85,
                threshold=6.75,
                is_anomaly=True,
                metric_names=["error_rate", "request_latency_ms"],
                details={},
                dedup_hash=compute_anomaly_dedup_hash(
                    "checkout-service", "lstm_autoencoder", anom_ts
                ),
            ),
            Anomaly(
                service_id=checkout_svc.id,
                detector_name="lstm_autoencoder",
                timestamp_utc=normal_ts,
                score=0.42,
                threshold=6.75,
                is_anomaly=False,
                metric_names=["error_rate"],
                details={},
                dedup_hash=compute_anomaly_dedup_hash(
                    "checkout-service", "lstm_autoencoder", normal_ts
                ),
            ),
            Incident(
                external_id="inc-graph-001",
                title="Checkout 5xx spike following payment timeouts",
                service_id=checkout_svc.id,
                severity="CRITICAL",
                status="open",
                started_at=window_start + timedelta(minutes=12),
            ),
        ]
    )
    db_session.commit()

    res = client.post(
        "/api/v1/correlation/timeline",
        json={
            "service": "checkout-service",
            "start_time": window_start.isoformat(),
            "end_time": window_end.isoformat(),
            "lookback_minutes": 60,
            "max_depth": 3,
        },
    )
    assert res.status_code == 200
    body = res.json()

    assert body["target_service"] == "checkout-service"
    assert body["lookback_minutes"] == 60
    assert body["related_services"] == ["api-gateway", "payment-service"]
    assert body["correlated_services"] == ["api-gateway", "checkout-service", "payment-service"]
    assert body["deployment_count"] == 1
    assert body["anomaly_count"] == 1
    assert body["event_count"] == 1
    assert body["incident_count"] == 1
    assert body["correlation_note"] == DEFAULT_CORRELATION_NOTE
    assert "causation is not established" in body["correlation_note"]

    timeline = body["timeline"]
    assert len(timeline) == 4
    assert [item["signal_type"] for item in timeline] == [
        "deployment",
        "anomaly",
        "event",
        "incident",
    ]
    assert all(item["service"] != "analytics-worker" for item in timeline)

    # Check exact chronological timestamps and deployment relative offset
    timestamps = [item["timestamp_utc"] for item in timeline]
    assert timestamps == sorted(timestamps)
    assert timeline[0]["service"] == "payment-service"
    assert timeline[0]["relation"] == "dependency"
    assert timeline[0]["hop_distance"] == 1
    assert timeline[0]["minutes_relative_to_window_start"] == -30.0
    assert timeline[1]["service"] == "checkout-service"
    assert timeline[1]["relation"] == "target"
    assert timeline[2]["service"] == "payment-service"
    assert timeline[3]["service"] == "checkout-service"


def test_graph_and_correlation_validation_errors(
    client: TestClient,
    db_session: Session,
) -> None:
    _seed_standard_topology_traces(client)

    # Dependency graph time-range validation -> 422
    assert (
        client.get(
            "/api/v1/graph/dependencies",
            params={"start_time": "2026-11-20T14:00:00"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/graph/dependencies",
            params={
                "start_time": "2026-11-20T15:00:00Z",
                "end_time": "2026-11-20T14:00:00Z",
            },
        ).status_code
        == 422
    )

    # Service impact validation -> 422
    assert client.get("/api/v1/graph/services/!!!/impact").status_code == 422
    assert (
        client.get(
            "/api/v1/graph/services/checkout-service/impact",
            params={"max_depth": 0},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/graph/services/checkout-service/impact",
            params={"max_depth": 11},
        ).status_code
        == 422
    )

    # Correlation timeline validation -> 422 (naive timestamp, inverted window, bad bounds)
    assert (
        client.post(
            "/api/v1/correlation/timeline",
            json={
                "service": "checkout-service",
                "start_time": "2026-11-20T14:00:00",
                "end_time": "2026-11-20T14:30:00Z",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/correlation/timeline",
            json={
                "service": "checkout-service",
                "start_time": "2026-11-20T15:00:00Z",
                "end_time": "2026-11-20T14:00:00Z",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/correlation/timeline",
            json={
                "service": "checkout-service",
                "start_time": "2026-11-20T14:00:00Z",
                "end_time": "2026-11-20T14:30:00Z",
                "lookback_minutes": -1,
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/correlation/timeline",
            json={
                "service": "checkout-service",
                "start_time": "2026-11-20T14:00:00Z",
                "end_time": "2026-11-20T14:30:00Z",
                "max_depth": 0,
            },
        ).status_code
        == 422
    )

    # Unknown service on correlation timeline -> 404
    assert (
        client.post(
            "/api/v1/correlation/timeline",
            json={
                "service": "ghost-service",
                "start_time": "2026-11-20T14:00:00Z",
                "end_time": "2026-11-20T14:30:00Z",
            },
        ).status_code
        == 404
    )
