import ast
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.graph import (
    DEFAULT_CORRELATION_NOTE,
    AnomalyRecord,
    DeploymentRecord,
    EventRecord,
    HopService,
    IncidentRecord,
    ServiceEdgeInput,
    analyze_service_impact,
    build_service_dependency_graph,
    correlate_timeline,
    extract_service_edges,
    extract_service_nodes,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GRAPH_PKG_DIR = REPO_ROOT / "services" / "graph"
FORBIDDEN_IMPORTS = {"fastapi", "sqlalchemy", "psycopg", "alembic", "starlette", "pickle", "joblib"}


# ---------------------------------------------------------------------------
# 1. Graph construction, edge aggregation & self-loop exclusion
# ---------------------------------------------------------------------------


def test_build_graph_aggregates_edges_excludes_self_loops_and_preserves_nodes() -> None:
    edges = [
        ServiceEdgeInput(
            caller="API_Gateway",
            callee="checkout_service",
            call_count=10,
            error_count=1,
            avg_duration_ms=20.0,
        ),
        ServiceEdgeInput(
            caller="api-gateway",
            callee="checkout-service",
            call_count=30,
            error_count=3,
            avg_duration_ms=60.0,
        ),
        # Self-loop must NOT create an edge, but must preserve the valid node
        ServiceEdgeInput(
            caller="worker-self",
            callee="worker-self",
            call_count=5,
            error_count=0,
            avg_duration_ms=12.0,
        ),
    ]

    graph = build_service_dependency_graph(edges, services=["isolated-service"])

    assert extract_service_nodes(graph) == [
        "api-gateway",
        "checkout-service",
        "isolated-service",
        "worker-self",
    ]
    assert not graph.has_edge("worker-self", "worker-self")

    extracted = extract_service_edges(graph)
    assert len(extracted) == 1
    edge = extracted[0]
    assert edge.caller == "api-gateway"
    assert edge.callee == "checkout-service"
    assert edge.call_count == 40
    assert edge.error_count == 4
    assert math.isclose(edge.error_rate, 0.1, rel_tol=1e-12)
    # Weighted average: (10 * 20.0 + 30 * 60.0) / 40 = 2000 / 40 = 50.0
    assert math.isclose(edge.avg_duration_ms, 50.0, rel_tol=1e-12)


def test_service_edge_input_validation() -> None:
    with pytest.raises(ValidationError):
        ServiceEdgeInput(caller="a", callee="b", call_count=5, error_count=6)
    with pytest.raises(ValidationError):
        ServiceEdgeInput(caller="a", callee="b", call_count=1, avg_duration_ms=float("nan"))
    with pytest.raises(ValidationError):
        ServiceEdgeInput(caller="a", callee="b", call_count=0)


# ---------------------------------------------------------------------------
# 2. Multi-hop traversal & max_depth cutoff
# ---------------------------------------------------------------------------


def test_multi_hop_impact_analysis_and_max_depth_cutoff() -> None:
    edges = [
        ServiceEdgeInput(caller="api-gateway", callee="checkout-service", call_count=100),
        ServiceEdgeInput(caller="checkout-service", callee="payment-service", call_count=80),
        ServiceEdgeInput(caller="checkout-service", callee="order-service", call_count=90),
        ServiceEdgeInput(caller="payment-service", callee="postgres-primary", call_count=80),
        ServiceEdgeInput(caller="order-service", callee="postgres-primary", call_count=90),
    ]
    graph = build_service_dependency_graph(edges)

    # Focus on payment-service (middle of chain)
    impact_full = analyze_service_impact(graph, "payment-service", max_depth=3)
    assert impact_full.target_service == "payment-service"
    assert impact_full.dependencies == [
        HopService(service="postgres-primary", hop_distance=1),
    ]
    assert impact_full.affected_callers == [
        HopService(service="checkout-service", hop_distance=1),
        HopService(service="api-gateway", hop_distance=2),
    ]
    assert impact_full.all_related_services == [
        "api-gateway",
        "checkout-service",
        "postgres-primary",
    ]

    # Focus on api-gateway with max_depth=1 vs max_depth=2 vs max_depth=3
    gw_d1 = analyze_service_impact(graph, "api-gateway", max_depth=1)
    assert gw_d1.dependencies == [HopService(service="checkout-service", hop_distance=1)]
    assert gw_d1.affected_callers == []

    gw_d2 = analyze_service_impact(graph, "api-gateway", max_depth=2)
    assert gw_d2.dependencies == [
        HopService(service="checkout-service", hop_distance=1),
        HopService(service="order-service", hop_distance=2),
        HopService(service="payment-service", hop_distance=2),
    ]

    gw_d3 = analyze_service_impact(graph, "api-gateway", max_depth=3)
    assert gw_d3.dependencies == [
        HopService(service="checkout-service", hop_distance=1),
        HopService(service="order-service", hop_distance=2),
        HopService(service="payment-service", hop_distance=2),
        HopService(service="postgres-primary", hop_distance=3),
    ]

    # Focus on postgres-primary (leaf) with max_depth=2 cuts off api-gateway (hop 3)
    pg_d2 = analyze_service_impact(graph, "postgres-primary", max_depth=2)
    assert pg_d2.dependencies == []
    assert pg_d2.affected_callers == [
        HopService(service="order-service", hop_distance=1),
        HopService(service="payment-service", hop_distance=1),
        HopService(service="checkout-service", hop_distance=2),
    ]


# ---------------------------------------------------------------------------
# 3. Cycle safety
# ---------------------------------------------------------------------------


def test_cycle_safe_impact_and_bidirectional_correlation() -> None:
    edges = [
        ServiceEdgeInput(caller="svc-a", callee="svc-b", call_count=10),
        ServiceEdgeInput(caller="svc-b", callee="svc-a", call_count=10),
    ]
    graph = build_service_dependency_graph(edges)

    impact = analyze_service_impact(graph, "svc-a", max_depth=5)
    assert impact.dependencies == [HopService(service="svc-b", hop_distance=1)]
    assert impact.affected_callers == [HopService(service="svc-b", hop_distance=1)]
    assert impact.all_related_services == ["svc-b"]

    t0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    summary = correlate_timeline(
        target_service="svc-a",
        window_start=t0,
        window_end=t0 + timedelta(minutes=30),
        graph=graph,
        events=[
            EventRecord(
                service="svc-b",
                timestamp_utc=t0 + timedelta(minutes=5),
                message="retry timeout",
                severity="ERROR",
            )
        ],
    )
    assert len(summary.timeline) == 1
    assert summary.timeline[0].relation == "bidirectional"
    assert summary.timeline[0].hop_distance == 1


# ---------------------------------------------------------------------------
# 4. Timeline correlation (lookback, filtering, ordering, non-causation note)
# ---------------------------------------------------------------------------


def test_correlate_timeline_lookback_filtering_ordering_and_non_causation() -> None:
    edges = [
        ServiceEdgeInput(caller="api-gateway", callee="checkout-service", call_count=200),
        ServiceEdgeInput(caller="checkout-service", callee="payment-service", call_count=150),
        ServiceEdgeInput(caller="payment-service", callee="postgres-primary", call_count=150),
    ]
    graph = build_service_dependency_graph(edges, services=["unrelated-analytics"])

    window_start = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
    window_end = datetime(2026, 11, 20, 14, 30, tzinfo=UTC)

    deployments = [
        # Included: 45 minutes before window_start on dependency (payment-service)
        DeploymentRecord(
            deployment_id="dep-in-lookback",
            service="payment-service",
            timestamp_utc=window_start - timedelta(minutes=45),
            version="v1.9.0",
            status="succeeded",
        ),
        # Included: boundary at exactly -60m on target (checkout-service)
        DeploymentRecord(
            deployment_id="dep-boundary",
            service="checkout-service",
            timestamp_utc=window_start - timedelta(minutes=60),
            version="v2.4.0",
            status="rolled_back",
        ),
        # Excluded: -61m (outside 60m lookback)
        DeploymentRecord(
            deployment_id="dep-too-old",
            service="checkout-service",
            timestamp_utc=window_start - timedelta(minutes=61),
            version="v2.3.9",
            status="succeeded",
        ),
        # Excluded: unrelated service even though within window
        DeploymentRecord(
            deployment_id="dep-unrelated",
            service="unrelated-analytics",
            timestamp_utc=window_start - timedelta(minutes=10),
            version="v0.1.0",
            status="succeeded",
        ),
        # Excluded: beyond max_depth=1 (postgres-primary is 2 hops from checkout-service)
        DeploymentRecord(
            deployment_id="dep-too-deep",
            service="postgres-primary",
            timestamp_utc=window_start - timedelta(minutes=5),
            version="v16.1",
            status="succeeded",
        ),
    ]

    anomalies = [
        # Included: inside window on target
        AnomalyRecord(
            anomaly_id="anom-1",
            service="checkout-service",
            timestamp_utc=window_start + timedelta(minutes=5),
            detector_name="lstm_autoencoder",
            score=9.42,
            threshold=6.75,
            is_anomaly=True,
            metric_names=["error_rate", "request_latency_ms"],
        ),
        # Excluded: is_anomaly=False
        AnomalyRecord(
            anomaly_id="anom-normal",
            service="checkout-service",
            timestamp_utc=window_start + timedelta(minutes=6),
            detector_name="lstm_autoencoder",
            score=0.21,
            threshold=6.75,
            is_anomaly=False,
        ),
        # Excluded: before window_start (anomalies do NOT use deployment lookback)
        AnomalyRecord(
            anomaly_id="anom-early",
            service="checkout-service",
            timestamp_utc=window_start - timedelta(minutes=1),
            detector_name="isolation_forest",
            score=0.40,
            threshold=0.09,
            is_anomaly=True,
        ),
    ]

    events = [
        # Same timestamp as anom-1 to test deterministic tie-breaking (anomaly < event)
        EventRecord(
            event_id="evt-1",
            service="api-gateway",
            timestamp_utc=window_start + timedelta(minutes=5),
            severity="ERROR",
            message="upstream 502 from checkout-service",
            trace_id="trace-123",
        ),
        # Excluded: unrelated service
        EventRecord(
            event_id="evt-unrelated",
            service="unrelated-analytics",
            timestamp_utc=window_start + timedelta(minutes=10),
            severity="ERROR",
            message="unrelated error",
        ),
    ]

    incidents = [
        IncidentRecord(
            incident_id="inc-1",
            service="checkout-service",
            timestamp_utc=window_start + timedelta(minutes=12),
            title="Checkout elevated 5xx rate",
            severity="CRITICAL",
            status="open",
        ),
    ]

    summary = correlate_timeline(
        target_service="checkout-service",
        window_start=window_start,
        window_end=window_end,
        graph=graph,
        deployments=deployments,
        anomalies=anomalies,
        events=events,
        incidents=incidents,
        lookback_minutes=60,
        max_depth=1,
    )

    assert summary.target_service == "checkout-service"
    assert summary.deployment_window_start == window_start - timedelta(minutes=60)
    assert summary.correlated_services == ["api-gateway", "checkout-service", "payment-service"]
    assert summary.correlation_note == DEFAULT_CORRELATION_NOTE
    assert "causation is not established" in summary.correlation_note
    assert summary.counts_by_signal == {
        "deployment": 2,
        "anomaly": 1,
        "event": 1,
        "incident": 1,
    }

    assert [item.record_id for item in summary.timeline] == [
        "dep-boundary",
        "dep-in-lookback",
        "anom-1",
        "evt-1",
        "inc-1",
    ]
    assert summary.timeline[0].minutes_relative_to_window_start == -60.0
    assert summary.timeline[0].relation == "target"
    assert summary.timeline[0].hop_distance == 0
    assert summary.timeline[1].minutes_relative_to_window_start == -45.0
    assert summary.timeline[1].relation == "dependency"
    assert summary.timeline[1].hop_distance == 1
    assert summary.timeline[2].minutes_relative_to_window_start is None
    assert summary.timeline[3].relation == "affected_caller"
    assert summary.timeline[3].hop_distance == 1


# ---------------------------------------------------------------------------
# 5. Error handling
# ---------------------------------------------------------------------------


def test_error_handling_for_invalid_graph_and_correlation_inputs() -> None:
    graph = build_service_dependency_graph(
        [ServiceEdgeInput(caller="api-gateway", callee="checkout-service")]
    )
    t0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 20, 13, 0, tzinfo=UTC)
    naive_t0 = datetime(2026, 11, 20, 12, 0)

    with pytest.raises(ValueError, match="not present in the dependency graph"):
        analyze_service_impact(graph, "unknown-service")

    with pytest.raises(ValueError, match="max_depth"):
        analyze_service_impact(graph, "api-gateway", max_depth=0)

    with pytest.raises(ValueError, match="Naive datetime"):
        correlate_timeline(
            target_service="api-gateway",
            window_start=naive_t0,
            window_end=t1,
            graph=graph,
        )

    with pytest.raises(ValueError, match="window_start must be less than or equal to window_end"):
        correlate_timeline(
            target_service="api-gateway",
            window_start=t1,
            window_end=t0,
            graph=graph,
        )

    with pytest.raises(ValueError, match="lookback_minutes"):
        correlate_timeline(
            target_service="api-gateway",
            window_start=t0,
            window_end=t1,
            graph=graph,
            lookback_minutes=-5,
        )

    with pytest.raises(ValidationError, match="Naive datetime"):
        DeploymentRecord(service="api-gateway", timestamp_utc=naive_t0, version="v1.0.0")


# ---------------------------------------------------------------------------
# 6. AST import-boundary enforcement for services/graph/
# ---------------------------------------------------------------------------


def test_graph_package_has_no_framework_or_db_imports() -> None:
    py_files = sorted(GRAPH_PKG_DIR.rglob("*.py"))
    assert len(py_files) >= 4

    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        overlap = imported_roots & FORBIDDEN_IMPORTS
        assert not overlap, f"{path.relative_to(REPO_ROOT)} imported forbidden: {overlap}"
