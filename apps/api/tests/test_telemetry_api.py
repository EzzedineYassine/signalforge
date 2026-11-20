import ast
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from apps.api.src.models import Deployment, IngestionFailure, MetricPoint, Service, TraceSpan
from services.ingestion import compute_metric_dedup_hash, ingest_metric_batch

REPO_ROOT = Path(__file__).resolve().parents[3]


class TestMetricsApi:
    def test_metrics_json_ingest_dedup_and_query(
        self, client: TestClient, db_session: Session
    ) -> None:
        batch = [
            # 0: later timestamp first to prove GET returns chronological ASC order
            {
                "service": "Payment_Service",
                "metric_name": "HTTP-Request Latency",
                "timestamp": "2026-11-20T15:35:00+01:00",
                "value": 215.5,
                "unit": "ms",
                "labels": {"b": 2, "a": 1},
            },
            # 1: in-batch duplicate with reversed label key order and Z timestamp
            {
                "service": "payment-service",
                "metric_name": "http_request_latency",
                "timestamp": "2026-11-20T14:35:00Z",
                "value": 215.5,
                "unit": "ms",
                "labels": {"a": 1, "b": 2},
            },
            # 2: earlier timestamp on same service
            {
                "service": "payment-service",
                "metric_name": "http_request_latency",
                "timestamp": "2026-11-20T14:30:00Z",
                "value": 120.25,
                "unit": "ms",
                "labels": {"a": 1, "b": 2},
            },
            # 3: second service
            {
                "service": "checkout.api",
                "metric_name": "cpu.utilization",
                "timestamp": "2026-11-20T14:31:00Z",
                "value": 0.67,
                "unit": "ratio",
            },
        ]

        first = client.post("/api/v1/metrics/ingest", json={"metrics": batch})
        assert first.status_code == 200
        body = first.json()
        assert UUID(body["batch_id"])
        assert body["received"] == 4
        assert body["accepted"] == 3
        assert body["duplicates"] == 1
        assert body["rejected"] == 0
        assert body["errors"] == []

        # Cross-batch idempotency
        second = client.post("/api/v1/metrics/ingest", json={"metrics": batch})
        assert second.status_code == 200
        assert second.json()["accepted"] == 0
        assert second.json()["duplicates"] == 4
        assert db_session.scalar(select(func.count(MetricPoint.id))) == 3

        services = db_session.scalars(select(Service).order_by(Service.name)).all()
        assert [(s.name, s.is_auto_discovered) for s in services] == [
            ("checkout-api", True),
            ("payment-service", True),
        ]

        # GET /api/v1/metrics returns chronological order (timestamp_utc ASC)
        query = client.get(
            "/api/v1/metrics",
            params={
                "service": "Payment_Service",
                "metric_name": "HTTP-Request Latency",
            },
        )
        assert query.status_code == 200
        data = query.json()
        assert data["total"] == 2
        assert [item["value"] for item in data["items"]] == [120.25, 215.5]
        assert [item["timestamp_utc"] for item in data["items"]] == [
            "2026-11-20T14:30:00Z",
            "2026-11-20T14:35:00Z",
        ]

        # Time range + pagination
        ranged = client.get(
            "/api/v1/metrics",
            params={
                "start_time": "2026-11-20T14:30:00Z",
                "end_time": "2026-11-20T14:32:00Z",
                "limit": 1,
                "offset": 1,
            },
        )
        assert ranged.status_code == 200
        ranged_data = ranged.json()
        assert ranged_data["total"] == 2
        assert len(ranged_data["items"]) == 1
        assert ranged_data["items"][0]["service_name"] == "checkout-api"

    def test_metrics_partial_batch_failures_and_nan_rejection(
        self, client: TestClient, db_session: Session
    ) -> None:
        payload = {
            "metrics": [
                {
                    "service": "api",
                    "metric_name": "req_count",
                    "timestamp": "2026-11-20T14:30:00Z",
                    "value": 10,
                },
                {
                    "service": "api",
                    "metric_name": "req_count",
                    "timestamp": "2026-11-20T14:30:00",  # naive timestamp
                    "value": 5,
                },
                {
                    "service": "api",
                    "metric_name": "req_count",
                    "timestamp": "2026-11-20T14:31:00Z",
                    "value": True,  # bool rejected
                },
                "not-a-dict",
            ]
        }

        response = client.post("/api/v1/metrics/ingest", json=payload)
        assert response.status_code == 200
        body = response.json()
        batch_id = UUID(body["batch_id"])
        assert body["received"] == 4
        assert body["accepted"] == 1
        assert body["duplicates"] == 0
        assert body["rejected"] == 3
        assert [e["index"] for e in body["errors"]] == [1, 2, 3]
        assert body["errors"][2]["error_code"] == "INVALID_RECORD_TYPE"

        failures = db_session.scalars(
            select(IngestionFailure)
            .where(IngestionFailure.batch_id == batch_id)
            .order_by(IngestionFailure.record_index)
        ).all()
        assert [f.record_index for f in failures] == [1, 2, 3]

        # Direct pipeline test for NaN/inf float values (not representable in strict JSON)
        nan_result = ingest_metric_batch(
            db_session,
            [
                {
                    "service": "api",
                    "metric_name": "cpu",
                    "timestamp": "2026-11-20T14:32:00Z",
                    "value": float("nan"),
                },
                {
                    "service": "api",
                    "metric_name": "cpu",
                    "timestamp": "2026-11-20T14:33:00Z",
                    "value": float("inf"),
                },
            ],
        )
        assert nan_result.accepted == 0
        assert nan_result.rejected == 2

    def test_metric_dedup_hash_key_order_equivalence(self) -> None:
        ts = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)
        h1 = compute_metric_dedup_hash("api", "cpu", ts, {"a": 1, "b": 2})
        h2 = compute_metric_dedup_hash("api", "cpu", ts, {"b": 2, "a": 1})
        assert h1 == h2
        assert len(h1) == 64

    def test_metrics_csv_ingest_idempotent_and_mixed_rows(
        self, client: TestClient, db_session: Session
    ) -> None:
        csv_body = (
            "service,metric_name,timestamp,value,unit,labels\n"
            'api-gateway,http_latency,2026-11-20T14:00:00Z,45.2,ms,"{""route"": ""/checkout""}"\n'
            'api-gateway,http_latency,2026-11-20T14:01:00Z,51.8,ms,"{""route"": ""/checkout""}"\n'
        )

        first = client.post(
            "/api/v1/metrics/ingest/csv",
            content=csv_body.encode("utf-8"),
            headers={"Content-Type": "text/csv"},
        )
        assert first.status_code == 200
        assert first.json()["received"] == 2
        assert first.json()["accepted"] == 2
        assert first.json()["duplicates"] == 0
        assert first.json()["rejected"] == 0

        # Idempotent re-upload
        second = client.post(
            "/api/v1/metrics/ingest/csv",
            content=csv_body.encode("utf-8"),
            headers={"Content-Type": "text/csv"},
        )
        assert second.status_code == 200
        assert second.json()["accepted"] == 0
        assert second.json()["duplicates"] == 2

        # Mixed valid + invalid CSV rows
        mixed_csv = (
            "service,metric_name,timestamp,value,unit,labels\n"
            "api-gateway,http_latency,2026-11-20T14:02:00Z,60.0,ms,\n"
            "api-gateway,http_latency,2026-11-20T14:03:00Z,not-a-float,ms,\n"
            "api-gateway,http_latency,2026-11-20T14:04:00,12.0,ms,\n"
            "api-gateway,http_latency,2026-11-20T14:05:00Z,15.0,ms,{bad-json\n"
        )
        mixed = client.post(
            "/api/v1/metrics/ingest/csv",
            content=mixed_csv.encode("utf-8"),
            headers={"Content-Type": "text/csv"},
        )
        assert mixed.status_code == 200
        mixed_body = mixed.json()
        batch_id = UUID(mixed_body["batch_id"])
        assert mixed_body["received"] == 4
        assert mixed_body["accepted"] == 1
        assert mixed_body["rejected"] == 3
        assert [e["index"] for e in mixed_body["errors"]] == [1, 2, 3]

        failures = db_session.scalars(
            select(IngestionFailure)
            .where(IngestionFailure.batch_id == batch_id)
            .order_by(IngestionFailure.record_index)
        ).all()
        assert [f.record_index for f in failures] == [1, 2, 3]

    @pytest.mark.parametrize(
        "bad_csv",
        [
            b"",
            b"   \n",
            b"service,metric_name,timestamp\napi,cpu,2026-11-20T14:00:00Z\n",
            b"service,metric_name,timestamp,value\n",
            b"\xff\xfeinvalid-utf8",
        ],
    )
    def test_metrics_csv_header_or_format_errors_return_422(
        self, client: TestClient, db_session: Session, bad_csv: bytes
    ) -> None:
        response = client.post(
            "/api/v1/metrics/ingest/csv",
            content=bad_csv,
            headers={"Content-Type": "text/csv"},
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "params",
        [
            {"start_time": "2026-11-20T14:00:00"},
            {"start_time": "2026-11-20T15:00:00Z", "end_time": "2026-11-20T14:00:00Z"},
            {"service": "$$$"},
            {"metric_name": "bad/metric"},
        ],
    )
    def test_metrics_query_invalid_params_return_422(
        self, client: TestClient, db_session: Session, params: dict[str, object]
    ) -> None:
        assert client.get("/api/v1/metrics", params=params).status_code == 422


class TestTracesApi:
    def test_traces_ingest_dedup_failures_and_query(
        self, client: TestClient, db_session: Session
    ) -> None:
        spans = [
            # 0: root server span (duration auto-computed = 250.0ms)
            {
                "service": "api_gateway",
                "trace_id": "trace-abc-1",
                "span_id": "span-root",
                "operation_name": "POST /v1/checkout",
                "span_kind": "server",
                "status_code": "ok",
                "start_time": "2026-11-20T14:00:00.000Z",
                "end_time": "2026-11-20T14:00:00.250Z",
                "attributes": {"http.method": "POST"},
            },
            # 1: child error span on second service
            {
                "service": "payment.service",
                "trace_id": "trace-abc-1",
                "span_id": "span-child",
                "parent_span_id": "span-root",
                "operation_name": "charge_card",
                "span_kind": "CLIENT",
                "status_code": "ERROR",
                "start_time": "2026-11-20T14:00:00.050Z",
                "end_time": "2026-11-20T14:00:00.200Z",
                "duration_ms": 150.0,
                "attributes": {"error": True},
            },
            # 2: in-batch duplicate of span-child
            {
                "service": "payment-service",
                "trace_id": "trace-abc-1",
                "span_id": "span-child",
                "parent_span_id": "span-root",
                "operation_name": "charge_card",
                "start_time": "2026-11-20T14:00:00.050Z",
                "end_time": "2026-11-20T14:00:00.200Z",
            },
            # 3: invalid end_time < start_time
            {
                "service": "payment-service",
                "trace_id": "trace-abc-1",
                "span_id": "span-bad-time",
                "operation_name": "broken",
                "start_time": "2026-11-20T14:00:05Z",
                "end_time": "2026-11-20T14:00:01Z",
            },
            # 4: invalid negative duration
            {
                "service": "payment-service",
                "trace_id": "trace-abc-1",
                "span_id": "span-neg-dur",
                "operation_name": "broken",
                "start_time": "2026-11-20T14:00:00Z",
                "end_time": "2026-11-20T14:00:01Z",
                "duration_ms": -5.0,
            },
        ]

        first = client.post("/api/v1/traces/ingest", json={"spans": spans})
        assert first.status_code == 200
        body = first.json()
        batch_id = UUID(body["batch_id"])
        assert body["received"] == 5
        assert body["accepted"] == 2
        assert body["duplicates"] == 1
        assert body["rejected"] == 2
        assert [e["index"] for e in body["errors"]] == [3, 4]

        # Failures persisted in ingestion_failures
        failures = db_session.scalars(
            select(IngestionFailure)
            .where(IngestionFailure.batch_id == batch_id)
            .order_by(IngestionFailure.record_index)
        ).all()
        assert [f.record_index for f in failures] == [3, 4]

        # Cross-batch deduplication
        second = client.post("/api/v1/traces/ingest", json={"spans": spans[:2]})
        assert second.status_code == 200
        assert second.json()["accepted"] == 0
        assert second.json()["duplicates"] == 2
        assert db_session.scalar(select(func.count(TraceSpan.id))) == 2

        # Query by trace_id (ordered by start_time_utc DESC)
        by_trace = client.get("/api/v1/traces", params={"trace_id": "trace-abc-1"})
        assert by_trace.status_code == 200
        trace_data = by_trace.json()
        assert trace_data["total"] == 2
        assert [s["span_id"] for s in trace_data["items"]] == ["span-child", "span-root"]
        root = trace_data["items"][1]
        assert root["duration_ms"] == pytest.approx(250.0)
        assert root["span_kind"] == "SERVER"
        assert root["status_code"] == "OK"
        assert root["parent_span_id"] is None

        # Filter by service + status_code + time range
        err_resp = client.get(
            "/api/v1/traces",
            params={
                "service": "Payment_Service",
                "status_code": "error",
                "start_time": "2026-11-20T14:00:00.010Z",
                "end_time": "2026-11-20T14:00:01Z",
            },
        )
        assert err_resp.status_code == 200
        assert err_resp.json()["total"] == 1
        assert err_resp.json()["items"][0]["span_id"] == "span-child"

    @pytest.mark.parametrize(
        "params",
        [
            {"status_code": "FAILED"},
            {"start_time": "2026-11-20T14:00:00"},
            {"start_time": "2026-11-20T15:00:00Z", "end_time": "2026-11-20T14:00:00Z"},
            {"trace_id": "   "},
        ],
    )
    def test_traces_query_invalid_params_return_422(
        self, client: TestClient, db_session: Session, params: dict[str, object]
    ) -> None:
        assert client.get("/api/v1/traces", params=params).status_code == 422


class TestDeploymentsApi:
    def test_deployments_ingest_and_query(
        self, client: TestClient, db_session: Session
    ) -> None:
        deployments = [
            {
                "service": "Checkout_Service",
                "version": "v2.4.0",
                "commit_sha": "abc1234",
                "status": "SUCCEEDED",
                "deployed_by": "ci-bot",
                "deployed_at": "2026-11-20T13:00:00Z",
                "metadata": {"pipeline": "github-actions"},
            },
            {
                "service": "payment.service",
                "version": "v1.9.1",
                "status": "rolled_back",
                "deployed_at": "2026-11-20T14:15:00Z",
            },
            # invalid status
            {
                "service": "payment-service",
                "version": "v1.9.2",
                "status": "unknown_state",
                "deployed_at": "2026-11-20T14:20:00Z",
            },
            # naive deployed_at
            {
                "service": "payment-service",
                "version": "v1.9.3",
                "status": "started",
                "deployed_at": "2026-11-20T14:25:00",
            },
        ]

        response = client.post("/api/v1/deployments/ingest", json={"deployments": deployments})
        assert response.status_code == 200
        body = response.json()
        batch_id = UUID(body["batch_id"])
        assert body["received"] == 4
        assert body["accepted"] == 2
        assert body["duplicates"] == 0
        assert body["rejected"] == 2
        assert [e["index"] for e in body["errors"]] == [2, 3]

        assert db_session.scalar(select(func.count(Deployment.id))) == 2
        failures = db_session.scalars(
            select(IngestionFailure)
            .where(IngestionFailure.batch_id == batch_id)
            .order_by(IngestionFailure.record_index)
        ).all()
        assert [f.record_index for f in failures] == [2, 3]

        # Ordered by deployed_at DESC
        all_resp = client.get("/api/v1/deployments")
        assert all_resp.status_code == 200
        all_data = all_resp.json()
        assert all_data["total"] == 2
        assert [(d["service_name"], d["version"], d["status"]) for d in all_data["items"]] == [
            ("payment-service", "v1.9.1", "rolled_back"),
            ("checkout-service", "v2.4.0", "succeeded"),
        ]
        assert all_data["items"][1]["metadata_json"] == {"pipeline": "github-actions"}

        # Filter by service, status, and time range
        filtered = client.get(
            "/api/v1/deployments",
            params={
                "service": "checkout-service",
                "status": "SUCCEEDED",
                "start_time": "2026-11-20T12:30:00Z",
                "end_time": "2026-11-20T13:30:00Z",
            },
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["version"] == "v2.4.0"

    @pytest.mark.parametrize(
        "params",
        [
            {"status": "cancelled"},
            {"start_time": "2026-11-20T13:00:00"},
            {"start_time": "2026-11-20T15:00:00Z", "end_time": "2026-11-20T13:00:00Z"},
            {"service": "!!!"},
        ],
    )
    def test_deployments_query_invalid_params_return_422(
        self, client: TestClient, db_session: Session, params: dict[str, object]
    ) -> None:
        assert client.get("/api/v1/deployments", params=params).status_code == 422


def test_telemetry_db_error_rolls_back_and_returns_500(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: Session) -> None:
        raise OperationalError("COMMIT", {}, Exception("simulated DB failure"))

    monkeypatch.setattr(Session, "commit", fail_commit)

    r_metrics = client.post(
        "/api/v1/metrics/ingest",
        json={
            "metrics": [
                {
                    "service": "api",
                    "metric_name": "cpu",
                    "timestamp": "2026-11-20T14:00:00Z",
                    "value": 0.5,
                }
            ]
        },
    )
    assert r_metrics.status_code == 500

    r_traces = client.post(
        "/api/v1/traces/ingest",
        json={
            "spans": [
                {
                    "service": "api",
                    "trace_id": "t1",
                    "span_id": "s1",
                    "operation_name": "op",
                    "start_time": "2026-11-20T14:00:00Z",
                    "end_time": "2026-11-20T14:00:01Z",
                }
            ]
        },
    )
    assert r_traces.status_code == 500

    r_deps = client.post(
        "/api/v1/deployments/ingest",
        json={
            "deployments": [
                {
                    "service": "api",
                    "version": "v1",
                    "status": "started",
                    "deployed_at": "2026-11-20T14:00:00Z",
                }
            ]
        },
    )
    assert r_deps.status_code == 500


def test_telemetry_module_import_boundaries() -> None:
    validator_path = REPO_ROOT / "services" / "ingestion" / "telemetry_validator.py"
    pipeline_path = REPO_ROOT / "services" / "ingestion" / "telemetry_pipeline.py"

    def _imports(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    assert _imports(validator_path).isdisjoint(
        {"fastapi", "sqlalchemy", "psycopg", "alembic", "starlette"}
    )
    assert _imports(pipeline_path).isdisjoint({"fastapi", "starlette"})
