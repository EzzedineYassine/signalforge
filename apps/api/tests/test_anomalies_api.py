from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from apps.api.src.models import Anomaly, Service
from apps.api.tests.conftest import alembic_config
from services.anomaly_detection import FEATURE_COLUMNS


def _build_service_metrics(
    service: str = "checkout-service",
    start: datetime = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC),
    normal_minutes: int = 35,
    spike_minutes: int = 5,
) -> tuple[list[dict[str, object]], datetime, datetime]:
    """Generate 7-feature aligned normal baseline rows followed by an extreme multi-metric spike."""
    points: list[dict[str, object]] = []
    normal_values = {
        "cpu_utilization": 0.38,
        "memory_utilization": 0.46,
        "request_latency_ms": 62.0,
        "request_throughput": 240.0,
        "error_rate": 0.004,
        "db_connections": 34.0,
        "queue_depth": 20.0,
    }
    spike_values = {
        "cpu_utilization": 0.98,
        "memory_utilization": 0.96,
        "request_latency_ms": 850.0,
        "request_throughput": 75.0,
        "error_rate": 0.45,
        "db_connections": 190.0,
        "queue_depth": 720.0,
    }

    total_minutes = normal_minutes + spike_minutes
    for minute in range(total_minutes):
        ts = (start + timedelta(minutes=minute)).isoformat()
        vals = normal_values if minute < normal_minutes else spike_values
        for feature in FEATURE_COLUMNS:
            points.append(
                {
                    "service": service,
                    "metric_name": feature,
                    "timestamp": ts,
                    "value": vals[feature],
                }
            )

    end = start + timedelta(minutes=total_minutes - 1)
    return points, start, end


class TestMigration0003Anomalies:
    def test_anomalies_table_indexes_and_reversibility(self, test_engine: Engine) -> None:
        insp = inspect(test_engine)
        assert "anomalies" in set(insp.get_table_names())
        assert {
            "ix_anomalies_service_id_timestamp_utc",
            "ix_anomalies_detector_name_is_anomaly_timestamp_utc",
        } <= {i["name"] for i in insp.get_indexes("anomalies")}
        assert "uq_anomalies_dedup_hash" in {
            u["name"] for u in insp.get_unique_constraints("anomalies")
        }
        assert "fk_anomalies_service_id_services" in {
            f["name"] for f in insp.get_foreign_keys("anomalies")
        }

        cfg = alembic_config()
        try:
            command.downgrade(cfg, "0002_telemetry_signals")
            tables_after_down = set(inspect(test_engine).get_table_names())
            assert "anomalies" not in tables_after_down
            assert {"services", "events", "metric_points", "trace_spans"} <= tables_after_down
        finally:
            command.upgrade(cfg, "head")
        assert "anomalies" in set(inspect(test_engine).get_table_names())


class TestAnomaliesApi:
    @pytest.mark.parametrize(
        "detector",
        ["lstm_autoencoder", "rolling_zscore", "isolation_forest"],
    )
    def test_detect_and_idempotent_re_run_all_detectors(
        self,
        client: TestClient,
        db_session: Session,
        detector: str,
    ) -> None:
        points, start, end = _build_service_metrics()
        ingest_resp = client.post("/api/v1/metrics/ingest", json={"metrics": points})
        assert ingest_resp.status_code == 200
        assert ingest_resp.json()["accepted"] == len(points)

        req_body = {
            "service": "Checkout_Service",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "detector": detector,
        }

        first = client.post("/api/v1/anomalies/detect", json=req_body)
        assert first.status_code == 200
        body1 = first.json()
        assert body1["service"] == "checkout-service"
        assert body1["detector"] == detector
        assert body1["points_evaluated"] == 40
        assert body1["anomalies_detected"] > 0
        assert body1["persisted"] == body1["anomalies_detected"]
        assert body1["duplicates"] == 0
        assert body1["max_score"] >= body1["threshold"]
        assert len(body1["items"]) == body1["anomalies_detected"]

        first_item = body1["items"][0]
        assert first_item["service_name"] == "checkout-service"
        assert first_item["detector_name"] == detector
        assert first_item["is_anomaly"] is True
        assert first_item["metric_names"] == list(FEATURE_COLUMNS)
        assert first_item["details"]["top_feature"] in FEATURE_COLUMNS
        assert set(first_item["details"]["feature_values"]) == set(FEATURE_COLUMNS)

        # Second call on the exact same service/window/detector must be strictly idempotent.
        second = client.post("/api/v1/anomalies/detect", json=req_body)
        assert second.status_code == 200
        body2 = second.json()
        assert body2["persisted"] == 0
        assert body2["duplicates"] == body1["anomalies_detected"]
        assert len(body2["items"]) == body1["anomalies_detected"]
        assert (
            db_session.scalar(select(func.count(Anomaly.id))) == body1["anomalies_detected"]
        )

    def test_query_anomalies_filters_pagination_and_persist_all(
        self, client: TestClient, db_session: Session
    ) -> None:
        empty = client.get("/api/v1/anomalies")
        assert empty.status_code == 200
        assert empty.json() == {"total": 0, "limit": 50, "offset": 0, "items": []}

        points, start, end = _build_service_metrics(
            service="payment-service", normal_minutes=15, spike_minutes=5
        )
        assert client.post("/api/v1/metrics/ingest", json={"metrics": points}).status_code == 200

        # Persist all 20 evaluated rows (both normal and anomalous) for lstm_autoencoder
        resp_lstm = client.post(
            "/api/v1/anomalies/detect",
            json={
                "service": "payment-service",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "detector": "lstm_autoencoder",
                "persist_all": True,
            },
        )
        assert resp_lstm.status_code == 200
        assert resp_lstm.json()["persisted"] == 20
        detected_lstm = resp_lstm.json()["anomalies_detected"]
        assert 0 < detected_lstm < 20

        # Also run rolling_zscore on the same window (anomalies only)
        resp_rz = client.post(
            "/api/v1/anomalies/detect",
            json={
                "service": "payment-service",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "detector": "rolling_zscore",
            },
        )
        assert resp_rz.status_code == 200

        # Filter by service + detector + is_anomaly=True
        q_anom = client.get(
            "/api/v1/anomalies",
            params={
                "service": "Payment_Service",
                "detector": "lstm_autoencoder",
                "is_anomaly": "true",
            },
        )
        assert q_anom.status_code == 200
        data_anom = q_anom.json()
        assert data_anom["total"] == detected_lstm
        assert all(item["is_anomaly"] is True for item in data_anom["items"])
        ts_list = [item["timestamp_utc"] for item in data_anom["items"]]
        assert ts_list == sorted(ts_list, reverse=True)

        # Filter by is_anomaly=False
        q_norm = client.get(
            "/api/v1/anomalies",
            params={"detector": "lstm_autoencoder", "is_anomaly": "false"},
        )
        assert q_norm.status_code == 200
        assert q_norm.json()["total"] == 20 - detected_lstm

        # Filter by time range + pagination
        q_page = client.get(
            "/api/v1/anomalies",
            params={
                "detector": "lstm_autoencoder",
                "start_time": (start + timedelta(minutes=10)).isoformat(),
                "end_time": end.isoformat(),
                "limit": 3,
                "offset": 2,
            },
        )
        assert q_page.status_code == 200
        page_data = q_page.json()
        assert page_data["total"] == 10
        assert len(page_data["items"]) == 3
        assert page_data["limit"] == 3
        assert page_data["offset"] == 2

    def test_detect_and_query_error_handling(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        start = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
        end = datetime(2026, 11, 20, 13, 0, 0, tzinfo=UTC)

        # Unknown service -> HTTP 404
        r_404 = client.post(
            "/api/v1/anomalies/detect",
            json={
                "service": "unknown-svc",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
            },
        )
        assert r_404.status_code == 404

        # Registered service with incomplete metrics (only 1 of 7 features) -> HTTP 422
        client.post(
            "/api/v1/metrics/ingest",
            json={
                "metrics": [
                    {
                        "service": "partial-svc",
                        "metric_name": "cpu_utilization",
                        "timestamp": start.isoformat(),
                        "value": 0.5,
                    }
                ]
            },
        )
        r_no_aligned = client.post(
            "/api/v1/anomalies/detect",
            json={
                "service": "partial-svc",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
            },
        )
        assert r_no_aligned.status_code == 422

        # Naive timestamps, inverted range, invalid detector -> HTTP 422
        for bad_body in [
            {
                "service": "partial-svc",
                "start_time": "2026-11-20T12:00:00",
                "end_time": end.isoformat(),
            },
            {
                "service": "partial-svc",
                "start_time": end.isoformat(),
                "end_time": start.isoformat(),
            },
            {
                "service": "partial-svc",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "detector": "bogus_detector",
            },
        ]:
            assert client.post("/api/v1/anomalies/detect", json=bad_body).status_code == 422

        # Invalid GET query params -> HTTP 422
        for bad_params in [
            {"detector": "not-a-detector"},
            {"start_time": "2026-11-20T12:00:00"},
            {"start_time": end.isoformat(), "end_time": start.isoformat()},
            {"service": "$$$"},
        ]:
            assert client.get("/api/v1/anomalies", params=bad_params).status_code == 422

        # Unexpected SQLAlchemyError rolls back cleanly and returns HTTP 500
        points, s2, e2 = _build_service_metrics(
            service="db-err-svc", normal_minutes=10, spike_minutes=3
        )
        assert client.post("/api/v1/metrics/ingest", json={"metrics": points}).status_code == 200

        def fail_commit(self: Session) -> None:
            raise OperationalError("COMMIT", {}, Exception("simulated failure"))

        monkeypatch.setattr(Session, "commit", fail_commit)
        r_500 = client.post(
            "/api/v1/anomalies/detect",
            json={
                "service": "db-err-svc",
                "start_time": s2.isoformat(),
                "end_time": e2.isoformat(),
                "detector": "rolling_zscore",
            },
        )
        assert r_500.status_code == 500
        assert db_session.scalar(select(func.count(Service.id))) == 2
        assert db_session.scalar(select(func.count(Anomaly.id))) == 0
