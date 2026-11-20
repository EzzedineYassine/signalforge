import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.src.models import Incident, Service

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_EVENTS_PATH = REPO_ROOT / "data" / "samples" / "sample_events.json"


def _ingest_sample_events(client: TestClient) -> dict[str, object]:
    payload = json.loads(SAMPLE_EVENTS_PATH.read_text(encoding="utf-8"))
    response = client.post("/api/v1/events/ingest", json=payload)
    assert response.status_code == 200
    return response.json()


class TestQueryEvents:
    def test_empty_db_returns_zero_total(self, client: TestClient, db_session: Session) -> None:
        response = client.get("/api/v1/events")
        assert response.status_code == 200
        assert response.json() == {"total": 0, "limit": 50, "offset": 0, "items": []}

    def test_filters_ordering_and_pagination(
        self, client: TestClient, db_session: Session
    ) -> None:
        ingest_body = _ingest_sample_events(client)
        assert ingest_body["accepted"] == 7

        # Default ordering is timestamp_utc DESC.
        all_resp = client.get("/api/v1/events")
        assert all_resp.status_code == 200
        all_data = all_resp.json()
        assert all_data["total"] == 7
        assert len(all_data["items"]) == 7
        timestamps = [item["timestamp_utc"] for item in all_data["items"]]
        assert timestamps == sorted(timestamps, reverse=True)

        # Unnormalized service query param is normalized cleanly.
        svc_resp = client.get("/api/v1/events", params={"service": "Checkout_Service "})
        assert svc_resp.status_code == 200
        svc_data = svc_resp.json()
        assert svc_data["total"] == 2
        assert all(item["service_name"] == "checkout-service" for item in svc_data["items"])

        # Valid service format with no matching rows returns empty result (not 422).
        missing_resp = client.get("/api/v1/events", params={"service": "nonexistent-service"})
        assert missing_resp.status_code == 200
        assert missing_resp.json()["total"] == 0
        assert missing_resp.json()["items"] == []

        # Severity alias normalization (err -> ERROR).
        sev_resp = client.get("/api/v1/events", params={"severity": "err"})
        assert sev_resp.status_code == 200
        sev_data = sev_resp.json()
        assert sev_data["total"] == 2
        assert all(item["severity"] == "ERROR" for item in sev_data["items"])

        # Event type filter (case-insensitive normalization).
        et_resp = client.get("/api/v1/events", params={"event_type": "HTTP_ERROR"})
        assert et_resp.status_code == 200
        assert et_resp.json()["total"] == 2

        # Trace ID filter.
        trace_resp = client.get("/api/v1/events", params={"trace_id": "trace-9f8e7d6c5b4a"})
        assert trace_resp.status_code == 200
        assert trace_resp.json()["total"] == 6

        # Time range filter (using +01:00 offset equivalent to 14:25:01Z..14:25:06Z).
        range_resp = client.get(
            "/api/v1/events",
            params={
                "start_time": "2026-11-20T15:25:01+01:00",
                "end_time": "2026-11-20T14:25:06Z",
            },
        )
        assert range_resp.status_code == 200
        range_data = range_resp.json()
        assert range_data["total"] == 3
        assert [i["span_id"] for i in range_data["items"]] == [
            "span-pay-04",
            "span-pay-03",
            "span-chk-02",
        ]

        # Pagination: total stays full filtered count while items reflect limit/offset.
        page1 = client.get(
            "/api/v1/events",
            params={"trace_id": "trace-9f8e7d6c5b4a", "limit": 2, "offset": 0},
        ).json()
        page2 = client.get(
            "/api/v1/events",
            params={"trace_id": "trace-9f8e7d6c5b4a", "limit": 2, "offset": 2},
        ).json()
        assert page1["total"] == 6
        assert page2["total"] == 6
        assert page1["limit"] == 2
        assert page2["offset"] == 2
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        assert {i["id"] for i in page1["items"]}.isdisjoint({i["id"] for i in page2["items"]})

    @pytest.mark.parametrize(
        "params",
        [
            {"start_time": "2026-11-20T14:25:00"},
            {"end_time": "2026-11-20T14:25:00"},
            {
                "start_time": "2026-11-20T14:30:00Z",
                "end_time": "2026-11-20T14:25:00Z",
            },
            {"severity": "not-a-severity"},
            {"service": "$$$"},
            {"event_type": "   "},
            {"trace_id": "   "},
            {"limit": 0},
            {"limit": 501},
            {"offset": -1},
        ],
    )
    def test_invalid_query_params_return_422(
        self, client: TestClient, db_session: Session, params: dict[str, object]
    ) -> None:
        response = client.get("/api/v1/events", params=params)
        assert response.status_code == 422


class TestQueryServices:
    def test_services_aggregation_ordering_and_zero_event_service(
        self, client: TestClient, db_session: Session
    ) -> None:
        _ingest_sample_events(client)

        idle_service = Service(
            name="analytics-worker",
            environment="staging",
            owner_team="data-platform",
            is_auto_discovered=False,
        )
        db_session.add(idle_service)
        db_session.commit()

        response = client.get("/api/v1/services")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 4
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert [s["name"] for s in data["items"]] == [
            "analytics-worker",
            "api-gateway",
            "checkout-service",
            "payment-service",
        ]

        by_name = {s["name"]: s for s in data["items"]}
        assert by_name["analytics-worker"]["event_count"] == 0
        assert by_name["analytics-worker"]["last_seen_at"] is None
        assert by_name["analytics-worker"]["is_auto_discovered"] is False
        assert by_name["analytics-worker"]["owner_team"] == "data-platform"

        assert by_name["api-gateway"]["event_count"] == 2
        assert datetime.fromisoformat(by_name["api-gateway"]["last_seen_at"]) == datetime(
            2026, 11, 20, 14, 25, 8, tzinfo=UTC
        )

        assert by_name["checkout-service"]["event_count"] == 2
        assert datetime.fromisoformat(by_name["checkout-service"]["last_seen_at"]) == datetime(
            2026, 11, 20, 14, 25, 7, tzinfo=UTC
        )

        assert by_name["payment-service"]["event_count"] == 3
        assert datetime.fromisoformat(by_name["payment-service"]["last_seen_at"]) == datetime(
            2026, 11, 20, 14, 26, 12, tzinfo=UTC
        )

        # Pagination check
        paged = client.get("/api/v1/services", params={"limit": 2, "offset": 1}).json()
        assert paged["total"] == 4
        assert [s["name"] for s in paged["items"]] == ["api-gateway", "checkout-service"]


class TestQueryIncidents:
    def test_empty_db_returns_zero_total(self, client: TestClient, db_session: Session) -> None:
        response = client.get("/api/v1/incidents")
        assert response.status_code == 200
        assert response.json() == {"total": 0, "limit": 50, "offset": 0, "items": []}

    def test_incidents_filtering_and_nullable_service(
        self, client: TestClient, db_session: Session
    ) -> None:
        pay_svc = Service(name="payment-service")
        chk_svc = Service(name="checkout-service")
        db_session.add_all([pay_svc, chk_svc])
        db_session.flush()

        inc1 = Incident(
            external_id="INC-101",
            title="Payment processor timeout spike",
            description="Elevated 504s from primary card processor",
            service_id=pay_svc.id,
            severity="CRITICAL",
            status="open",
            started_at=datetime(2026, 11, 20, 14, 25, 0, tzinfo=UTC),
        )
        inc2 = Incident(
            external_id="INC-102",
            title="Checkout queue backlog",
            service_id=chk_svc.id,
            severity="WARNING",
            status="resolved",
            started_at=datetime(2026, 11, 20, 13, 0, 0, tzinfo=UTC),
            resolved_at=datetime(2026, 11, 20, 13, 45, 0, tzinfo=UTC),
        )
        inc3 = Incident(
            external_id="INC-103",
            title="Unattributed DNS degradation",
            service_id=None,
            severity="ERROR",
            status="open",
            started_at=datetime(2026, 11, 20, 15, 0, 0, tzinfo=UTC),
        )
        db_session.add_all([inc1, inc2, inc3])
        db_session.commit()

        # Unfiltered returns all 3 ordered by started_at DESC, including service_id=None.
        all_resp = client.get("/api/v1/incidents")
        assert all_resp.status_code == 200
        all_data = all_resp.json()
        assert all_data["total"] == 3
        assert [(i["external_id"], i["service_name"]) for i in all_data["items"]] == [
            ("INC-103", None),
            ("INC-101", "payment-service"),
            ("INC-102", "checkout-service"),
        ]

        # Filter by unnormalized service name.
        svc_resp = client.get("/api/v1/incidents", params={"service": "Payment_Service"})
        assert svc_resp.status_code == 200
        assert svc_resp.json()["total"] == 1
        assert svc_resp.json()["items"][0]["external_id"] == "INC-101"

        # Filter by status (case-insensitive).
        status_resp = client.get("/api/v1/incidents", params={"status": "OPEN"})
        assert status_resp.status_code == 200
        assert status_resp.json()["total"] == 2
        assert [i["external_id"] for i in status_resp.json()["items"]] == ["INC-103", "INC-101"]

        # Filter by severity alias (fatal -> CRITICAL).
        sev_resp = client.get("/api/v1/incidents", params={"severity": "fatal"})
        assert sev_resp.status_code == 200
        assert sev_resp.json()["total"] == 1
        assert sev_resp.json()["items"][0]["external_id"] == "INC-101"

        # Filter by time range.
        range_resp = client.get(
            "/api/v1/incidents",
            params={
                "start_time": "2026-11-20T14:00:00Z",
                "end_time": "2026-11-20T14:30:00Z",
            },
        )
        assert range_resp.status_code == 200
        assert range_resp.json()["total"] == 1
        assert range_resp.json()["items"][0]["external_id"] == "INC-101"

    @pytest.mark.parametrize(
        "params",
        [
            {"start_time": "2026-11-20T14:00:00"},
            {
                "start_time": "2026-11-20T15:00:00Z",
                "end_time": "2026-11-20T14:00:00Z",
            },
            {"severity": "bogus"},
            {"service": "!!!"},
            {"status": "   "},
        ],
    )
    def test_invalid_incident_params_return_422(
        self, client: TestClient, db_session: Session, params: dict[str, object]
    ) -> None:
        response = client.get("/api/v1/incidents", params=params)
        assert response.status_code == 422
