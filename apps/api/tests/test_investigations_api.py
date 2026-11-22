import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session

from apps.api.src.core.config import get_settings
from apps.api.src.main import app
from apps.api.src.models import Anomaly, Incident, Service
from apps.api.src.services.anomaly_service import compute_anomaly_dedup_hash
from apps.api.src.services.investigation_service import get_llm_client
from apps.api.src.services.knowledge_service import get_knowledge_retriever
from apps.api.tests.conftest import alembic_config
from services.investigation import (
    DETERMINISTIC_ABSTENTION_MODEL,
    FakeLLMClient,
    LLMClientError,
    LLMInvestigationDraft,
    RootCauseHypothesis,
)
from services.retrieval.chunking.markdown_chunker import MarkdownChunker
from services.retrieval.embeddings.embedder import DeterministicHashEmbedder
from services.retrieval.ingestion.loader import load_knowledge_base
from services.retrieval.search.hybrid import HybridRetriever
from services.retrieval.search.qdrant_store import QdrantVectorStore

REPO_ROOT = Path(__file__).resolve().parents[3]
SYNTHETIC_KB_DIR = REPO_ROOT / "data" / "synthetic" / "knowledge_base"
TEST_QDRANT_COLLECTION = "signalforge_knowledge_test"


@pytest.fixture
def indexed_test_retriever(tmp_path: Path) -> Iterator[HybridRetriever]:
    store = QdrantVectorStore(
        url=get_settings().QDRANT_URL,
        collection_name=TEST_QDRANT_COLLECTION,
    )
    store.delete_collection()
    retriever = HybridRetriever(
        embedder=DeterministicHashEmbedder(dimension=384),
        vector_store=store,
        bm25_path=tmp_path / "bm25_inv_test.json",
    )
    docs = load_knowledge_base(SYNTHETIC_KB_DIR)
    chunks = MarkdownChunker(max_chars=1200, overlap_chars=150).chunk_documents(docs)
    retriever.index_chunks(chunks)

    app.dependency_overrides[get_knowledge_retriever] = lambda: retriever
    try:
        yield retriever
    finally:
        app.dependency_overrides.pop(get_knowledge_retriever, None)
        store.delete_collection()


def _seed_incident_scenario(
    client: TestClient,
    db_session: Session,
) -> tuple[datetime, datetime]:
    """Seed traces, deployment, anomaly, error event, and incident for checkout-service."""
    window_start = datetime(2026, 11, 20, 14, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)

    trace_res = client.post(
        "/api/v1/traces/ingest",
        json={
            "spans": [
                {
                    "service": "api-gateway",
                    "trace_id": "trace-inv-1",
                    "span_id": "span-gw-1",
                    "parent_span_id": None,
                    "operation_name": "POST /v1/checkout",
                    "span_kind": "SERVER",
                    "status_code": "ERROR",
                    "start_time": window_start.isoformat(),
                    "end_time": (window_start + timedelta(milliseconds=180)).isoformat(),
                    "duration_ms": 180.0,
                },
                {
                    "service": "checkout-service",
                    "trace_id": "trace-inv-1",
                    "span_id": "span-co-1",
                    "parent_span_id": "span-gw-1",
                    "operation_name": "checkout.process",
                    "span_kind": "SERVER",
                    "status_code": "ERROR",
                    "start_time": (window_start + timedelta(milliseconds=5)).isoformat(),
                    "end_time": (window_start + timedelta(milliseconds=150)).isoformat(),
                    "duration_ms": 145.0,
                },
                {
                    "service": "payment-service",
                    "trace_id": "trace-inv-1",
                    "span_id": "span-pay-1",
                    "parent_span_id": "span-co-1",
                    "operation_name": "payment.authorize",
                    "span_kind": "SERVER",
                    "status_code": "ERROR",
                    "start_time": (window_start + timedelta(milliseconds=10)).isoformat(),
                    "end_time": (window_start + timedelta(milliseconds=140)).isoformat(),
                    "duration_ms": 130.0,
                },
            ]
        },
    )
    assert trace_res.status_code == 200
    assert trace_res.json()["accepted"] == 3

    dep_res = client.post(
        "/api/v1/deployments/ingest",
        json={
            "deployments": [
                {
                    "service": "payment-service",
                    "version": "v1.9.0",
                    "deployed_at": (window_start - timedelta(minutes=20)).isoformat(),
                    "status": "succeeded",
                }
            ]
        },
    )
    assert dep_res.status_code == 200

    evt_res = client.post(
        "/api/v1/events/ingest",
        json={
            "events": [
                {
                    "service": "payment-service",
                    "timestamp": (window_start + timedelta(minutes=6)).isoformat(),
                    "severity": "ERROR",
                    "message": "Primary card processor stripe-eu returned HTTP 504 timeout",
                    "event_type": "log",
                    "trace_id": "trace-inv-1",
                }
            ]
        },
    )
    assert evt_res.status_code == 200

    checkout_svc = db_session.scalar(select(Service).where(Service.name == "checkout-service"))
    assert checkout_svc is not None

    anom_ts = window_start + timedelta(minutes=5)
    db_session.add_all(
        [
            Anomaly(
                service_id=checkout_svc.id,
                detector_name="lstm_autoencoder",
                timestamp_utc=anom_ts,
                score=9.82,
                threshold=6.75,
                is_anomaly=True,
                metric_names=["error_rate", "request_latency_ms"],
                details={},
                dedup_hash=compute_anomaly_dedup_hash(
                    "checkout-service", "lstm_autoencoder", anom_ts
                ),
            ),
            Incident(
                external_id="inc-inv-test-001",
                title="Checkout 5xx outage correlated with payment processor 504",
                service_id=checkout_svc.id,
                severity="CRITICAL",
                status="open",
                started_at=window_start + timedelta(minutes=8),
            ),
        ]
    )
    db_session.commit()
    return window_start, window_end


def _extract_first_cited_doc_id(user_prompt: str) -> str:
    section = user_prompt.split("=== KNOWLEDGE CITATIONS ===", 1)[1]
    match = re.search(r"- \[([^\]]+)\]", section)
    assert match is not None, f"No citation doc_id found in prompt:\n{section}"
    return match.group(1)


class TestMigration0004Investigations:
    def test_investigations_table_indexes_and_reversibility(self, test_engine: Engine) -> None:
        insp = inspect(test_engine)
        assert "investigations" in set(insp.get_table_names())
        assert "ix_investigations_service_id_created_at" in {
            i["name"] for i in insp.get_indexes("investigations")
        }
        assert "fk_investigations_service_id_services" in {
            f["name"] for f in insp.get_foreign_keys("investigations")
        }

        cfg = alembic_config()
        try:
            command.downgrade(cfg, "0003_anomalies")
            tables_after_down = set(inspect(test_engine).get_table_names())
            assert "investigations" not in tables_after_down
            assert {"services", "anomalies"} <= tables_after_down
        finally:
            command.upgrade(cfg, "head")

        assert "investigations" in set(inspect(test_engine).get_table_names())


def test_investigation_run_persistence_validation_and_query_endpoints(
    client: TestClient,
    db_session: Session,
    indexed_test_retriever: HybridRetriever,
) -> None:
    window_start, window_end = _seed_incident_scenario(client, db_session)

    # Run 1: Clean grounded draft from FakeLLMClient
    def grounded_responder(_sys: str, user_prompt: str, _cls: type) -> LLMInvestigationDraft:
        valid_doc = _extract_first_cited_doc_id(user_prompt)
        return LLMInvestigationDraft(
            confirmed_facts=["payment-service logged stripe-eu HTTP 504 timeout at 14:06 UTC."],
            detected_anomalies=["checkout-service LSTM anomaly score 9.82 > 6.75."],
            observed_correlations=["payment-service v1.9.0 deployed 20m prior to spike."],
            probable_root_causes=[
                RootCauseHypothesis(
                    hypothesis="Upstream payment processor 504 outage cascaded to checkout.",
                    confidence="high",
                    supporting_evidence_ids=["ev-002", "ev-003"],
                )
            ],
            cited_doc_ids=[valid_doc],
            recommended_next_steps=["Fail over card traffic to secondary processor."],
            uncertainty_and_gaps=["External processor status feed not ingested."],
            abstained=False,
        )

    fake_llm_clean = FakeLLMClient(response=grounded_responder, model_name="fake-qwen2.5:7b")
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_clean
    try:
        res1 = client.post(
            "/api/v1/investigations/run",
            json={
                "question": "Why did checkout-service 5xx errors spike during payment failures?",
                "service": "checkout-service",
                "start_time": window_start.isoformat(),
                "end_time": window_end.isoformat(),
                "lookback_minutes": 60,
                "max_depth": 3,
                "top_k_docs": 5,
            },
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert res1.status_code == 200
    body1 = res1.json()
    assert len(fake_llm_clean.calls) == 1
    assert body1["service_name"] == "checkout-service"
    assert body1["abstained"] is False
    assert body1["model_name"] == "fake-qwen2.5:7b"
    assert body1["executed_nodes"] == [
        "plan_investigation",
        "collect_evidence",
        "synthesize_report",
        "validate_evidence",
    ]
    assert body1["duration_ms"] >= 0.0
    report1 = body1["report"]
    assert report1["abstained"] is False
    assert report1["validation_warnings"] == []
    assert len(report1["citations"]) == 1
    assert report1["citations"][0]["is_synthetic"] is True
    assert report1["citations"][0]["source_path"].endswith(".md")
    assert len(report1["probable_root_causes"]) == 1
    assert report1["probable_root_causes"][0]["is_proven_causation"] is False

    # Run 2: Draft with fabricated doc_id and fabricated evidence_id -> stripped with warnings
    def hallucinating_responder(_sys: str, user_prompt: str, _cls: type) -> LLMInvestigationDraft:
        valid_doc = _extract_first_cited_doc_id(user_prompt)
        return LLMInvestigationDraft(
            confirmed_facts=["checkout-service experienced errors."],
            probable_root_causes=[
                RootCauseHypothesis(
                    hypothesis="Payment timeout propagated to checkout.",
                    confidence="medium",
                    supporting_evidence_ids=["ev-002", "ev-999-fabricated"],
                )
            ],
            cited_doc_ids=[valid_doc, "rb-999-fabricated"],
            abstained=False,
        )

    fake_llm_halluc = FakeLLMClient(response=hallucinating_responder, model_name="fake-qwen2.5:7b")
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_halluc
    try:
        res2 = client.post(
            "/api/v1/investigations/run",
            json={
                "question": "Check payment failover impact on checkout-service",
                "service": "checkout-service",
                "start_time": window_start.isoformat(),
                "end_time": window_end.isoformat(),
            },
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert res2.status_code == 200
    body2 = res2.json()
    report2 = body2["report"]
    assert all(c["doc_id"] != "rb-999-fabricated" for c in report2["citations"])
    assert report2["probable_root_causes"][0]["supporting_evidence_ids"] == ["ev-002"]
    assert any("rb-999-fabricated" in w for w in report2["validation_warnings"])
    assert any("ev-999-fabricated" in w for w in report2["validation_warnings"])

    # Query persisted list & single investigation by ID
    list_res = client.get(
        "/api/v1/investigations",
        params={"service": "checkout-service", "abstained": "false"},
    )
    assert list_res.status_code == 200
    list_body = list_res.json()
    assert list_body["total"] == 2
    assert len(list_body["items"]) == 2
    assert list_body["items"][0]["id"] == body2["id"]
    assert list_body["items"][1]["id"] == body1["id"]

    get_res = client.get(f"/api/v1/investigations/{body1['id']}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == body1["id"]
    assert get_res.json()["report"]["citations"] == report1["citations"]


def test_zero_evidence_short_circuits_llm_and_abstains(
    client: TestClient,
    db_session: Session,
    indexed_test_retriever: HybridRetriever,
) -> None:
    db_session.add(Service(name="idle-service"))
    db_session.commit()

    fake_llm = FakeLLMClient(
        response=LLMInvestigationDraft(abstained=False),
        model_name="should-not-be-called",
    )
    app.dependency_overrides[get_llm_client] = lambda: fake_llm
    try:
        res = client.post(
            "/api/v1/investigations/run",
            json={
                "question": "Why did idle-service fail?",
                "service": "idle-service",
                "start_time": "2026-11-20T14:00:00Z",
                "end_time": "2026-11-20T14:30:00Z",
            },
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert res.status_code == 200
    body = res.json()
    assert len(fake_llm.calls) == 0
    assert body["abstained"] is True
    assert body["model_name"] == DETERMINISTIC_ABSTENTION_MODEL
    assert body["executed_nodes"] == [
        "plan_investigation",
        "collect_evidence",
        "abstain_no_evidence",
    ]
    assert body["report"]["abstained"] is True
    assert body["report"]["probable_root_causes"] == []
    assert body["report"]["abstention_reason"] is not None

    # Confirm abstained report was persisted cleanly
    fetched = client.get(f"/api/v1/investigations/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["abstained"] is True


def test_llm_error_produces_deterministic_fallback_report(
    client: TestClient,
    db_session: Session,
    indexed_test_retriever: HybridRetriever,
) -> None:
    window_start, window_end = _seed_incident_scenario(client, db_session)

    failing_llm = FakeLLMClient(
        error=LLMClientError("Unable to connect to Ollama service"),
        model_name="qwen2.5:7b",
    )
    app.dependency_overrides[get_llm_client] = lambda: failing_llm
    try:
        res = client.post(
            "/api/v1/investigations/run",
            json={
                "question": "Why did checkout-service fail with payment 504 timeouts?",
                "service": "checkout-service",
                "start_time": window_start.isoformat(),
                "end_time": window_end.isoformat(),
            },
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert res.status_code == 200
    body = res.json()
    assert len(failing_llm.calls) == 1
    assert body["abstained"] is True
    assert body["executed_nodes"] == [
        "plan_investigation",
        "collect_evidence",
        "synthesize_report",
        "validate_evidence",
    ]
    report = body["report"]
    assert report["abstained"] is True
    assert report["probable_root_causes"] == []
    assert "LLM synthesis unavailable" in report["abstention_reason"]
    assert len(report["confirmed_facts"]) >= 1
    assert len(report["detected_anomalies"]) >= 1
    assert len(report["observed_correlations"]) >= 1
    assert len(report["citations"]) >= 1


def test_qdrant_unavailable_still_completes_investigation(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    indexed_test_retriever: HybridRetriever,
) -> None:
    window_start, window_end = _seed_incident_scenario(client, db_session)

    def _raise_qdrant_down(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("Qdrant refused connection")

    monkeypatch.setattr(indexed_test_retriever.vector_store, "search", _raise_qdrant_down)

    fake_llm = FakeLLMClient(
        response=LLMInvestigationDraft(
            confirmed_facts=["payment-service logged 504 timeout."],
            probable_root_causes=[
                RootCauseHypothesis(
                    hypothesis="Payment timeout impacted checkout.",
                    confidence="medium",
                    supporting_evidence_ids=["ev-002"],
                )
            ],
            cited_doc_ids=[],
            abstained=False,
        ),
        model_name="fake-qwen2.5:7b",
    )
    app.dependency_overrides[get_llm_client] = lambda: fake_llm
    try:
        res = client.post(
            "/api/v1/investigations/run",
            json={
                "question": "Investigate checkout errors",
                "service": "checkout-service",
                "start_time": window_start.isoformat(),
                "end_time": window_end.isoformat(),
            },
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert res.status_code == 200
    report = res.json()["report"]
    assert report["abstained"] is False
    assert "Knowledge base unavailable during retrieval" in report["validation_warnings"]
    assert "Knowledge base unavailable during retrieval" in report["uncertainty_and_gaps"]


def test_investigation_api_404_and_422_errors(
    client: TestClient,
    db_session: Session,
    indexed_test_retriever: HybridRetriever,
) -> None:
    # Unknown service -> 404
    res_missing_svc = client.post(
        "/api/v1/investigations/run",
        json={
            "question": "Why did ghost-service fail?",
            "service": "ghost-service",
            "start_time": "2026-11-20T14:00:00Z",
            "end_time": "2026-11-20T14:30:00Z",
        },
    )
    assert res_missing_svc.status_code == 404

    # Unknown investigation UUID -> 404
    res_missing_id = client.get(f"/api/v1/investigations/{uuid.uuid4()}")
    assert res_missing_id.status_code == 404

    # Blank question -> 422
    assert (
        client.post(
            "/api/v1/investigations/run",
            json={
                "question": "   ",
                "service": "checkout-service",
                "start_time": "2026-11-20T14:00:00Z",
                "end_time": "2026-11-20T14:30:00Z",
            },
        ).status_code
        == 422
    )

    # Naive timestamp -> 422
    assert (
        client.post(
            "/api/v1/investigations/run",
            json={
                "question": "Why did checkout fail?",
                "service": "checkout-service",
                "start_time": "2026-11-20T14:00:00",
                "end_time": "2026-11-20T14:30:00Z",
            },
        ).status_code
        == 422
    )

    # Inverted window -> 422
    assert (
        client.post(
            "/api/v1/investigations/run",
            json={
                "question": "Why did checkout fail?",
                "service": "checkout-service",
                "start_time": "2026-11-20T15:00:00Z",
                "end_time": "2026-11-20T14:30:00Z",
            },
        ).status_code
        == 422
    )
