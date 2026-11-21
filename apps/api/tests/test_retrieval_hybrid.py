import json
import math
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.src.core.config import get_settings
from apps.api.src.main import app
from apps.api.src.services.knowledge_service import get_knowledge_retriever
from services.retrieval.chunking.markdown_chunker import DocumentChunk, MarkdownChunker
from services.retrieval.embeddings.embedder import (
    DeterministicHashEmbedder,
    EmbeddingProvider,
    SentenceTransformerEmbedder,
)
from services.retrieval.ingestion.loader import load_knowledge_base
from services.retrieval.search.bm25_index import BM25Index
from services.retrieval.search.hybrid import HybridRetriever, reciprocal_rank_fusion
from services.retrieval.search.models import RetrievedChunk
from services.retrieval.search.qdrant_store import QdrantVectorStore

REPO_ROOT = Path(__file__).resolve().parents[3]
SYNTHETIC_KB_DIR = REPO_ROOT / "data" / "synthetic" / "knowledge_base"
TEST_QDRANT_COLLECTION = "signalforge_knowledge_test"


def _make_chunk(
    chunk_id: str,
    doc_id: str,
    doc_type: str,
    title: str,
    service: str,
    section_heading: str,
    text: str,
    *,
    chunk_index: int = 0,
    source_path: str = "runbooks/sample.md",
    is_synthetic: bool = True,
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        doc_type=doc_type,  # type: ignore[arg-type]
        title=title,
        service=service,
        section_heading=section_heading,
        chunk_index=chunk_index,
        source_path=source_path,
        is_synthetic=is_synthetic,
        text=text,
    )


def _make_retrieved(
    chunk_id: str,
    *,
    doc_id: str = "rb-001",
    score: float = 0.5,
    source: str = "dense",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        doc_type="runbook",
        title="Sample Runbook",
        service="postgres-primary",
        section_heading="Triage",
        chunk_index=0,
        source_path="runbooks/sample.md",
        is_synthetic=True,
        text="Sample operational triage instructions.",
        score=score,
        retrieval_source=source,  # type: ignore[arg-type]
    )


@pytest.fixture
def qdrant_test_store() -> Iterator[QdrantVectorStore]:
    """Isolated Qdrant collection cleaned up before and after each integration test."""
    store = QdrantVectorStore(
        url=get_settings().QDRANT_URL,
        collection_name=TEST_QDRANT_COLLECTION,
    )
    store.delete_collection()
    try:
        yield store
    finally:
        store.delete_collection()


@pytest.fixture
def test_retriever(
    qdrant_test_store: QdrantVectorStore,
    tmp_path: Path,
) -> Iterator[HybridRetriever]:
    retriever = HybridRetriever(
        embedder=DeterministicHashEmbedder(dimension=384),
        vector_store=qdrant_test_store,
        bm25_path=tmp_path / "bm25_test_index.json",
    )
    app.dependency_overrides[get_knowledge_retriever] = lambda: retriever
    try:
        yield retriever
    finally:
        app.dependency_overrides.pop(get_knowledge_retriever, None)


# ---------------------------------------------------------------------------
# 1. Embedder & BM25Index unit tests
# ---------------------------------------------------------------------------


def test_deterministic_hash_embedder_protocol_and_cosine_semantics() -> None:
    embedder = DeterministicHashEmbedder(dimension=384)
    assert isinstance(embedder, EmbeddingProvider)
    assert embedder.dimension == 384

    v_a1 = embedder.embed_query("postgres-primary connection pool exhausted")
    v_a2 = embedder.embed_query("postgres-primary connection pool exhausted")
    v_b = embedder.embed_query("postgres connection pool saturation pgbouncer")
    v_c = embedder.embed_query("kafka consumer rebalance partition lag")

    assert v_a1 == v_a2
    assert math.isclose(math.sqrt(sum(x * x for x in v_a1)), 1.0, rel_tol=1e-6)

    sim_related = sum(x * y for x, y in zip(v_a1, v_b, strict=True))
    sim_unrelated = sum(x * y for x, y in zip(v_a1, v_c, strict=True))
    assert sim_related > sim_unrelated


def test_bm25_keyword_matching_filters_and_json_roundtrip(tmp_path: Path) -> None:
    chunks = [
        _make_chunk(
            chunk_id="00000000-0000-5000-8000-000000000001",
            doc_id="rb-002",
            doc_type="runbook",
            title="PostgreSQL Connection Pool Exhaustion Runbook",
            service="postgres-primary",
            section_heading="Error Codes",
            text="SQLSTATE 53300 too_many_connections and PgBouncer pool saturation.",
            source_path="runbooks/rb_002.md",
        ),
        _make_chunk(
            chunk_id="00000000-0000-5000-8000-000000000002",
            doc_id="rb-012",
            doc_type="runbook",
            title="Order Service Database Deadlock Recovery",
            service="order-service",
            section_heading="Deadlocks",
            text="SQLSTATE 40P01 deadlock_detected during concurrent inventory updates.",
            source_path="runbooks/rb_012.md",
        ),
        _make_chunk(
            chunk_id="00000000-0000-5000-8000-000000000003",
            doc_id="inc-2026-002",
            doc_type="incident",
            title="Incident Postmortem — Postgres Connection Pool Exhaustion",
            service="postgres-primary",
            section_heading="Root Cause",
            text="Connection leak caused SQLSTATE 53300 and queue timeouts across checkout.",
            source_path="incidents/inc_2026_002.md",
        ),
    ]

    index = BM25Index()
    assert index.search("40P01") == []

    # Idempotent upsert: indexing twice keeps size == 3
    index.upsert_chunks(chunks)
    assert index.size == 3
    index.upsert_chunks(chunks)
    assert index.size == 3

    # Exact error-code matching
    deadlock_hits = index.search("40P01 deadlock_detected", top_k=5)
    assert len(deadlock_hits) == 1
    assert deadlock_hits[0].doc_id == "rb-012"
    assert deadlock_hits[0].retrieval_source == "bm25"
    assert deadlock_hits[0].score > 0.0
    assert deadlock_hits[0].is_synthetic is True
    assert deadlock_hits[0].source_path == "runbooks/rb_012.md"

    # Metadata filtering by service and doc_type
    filtered = index.search(
        "53300 connection pool",
        top_k=5,
        service="postgres-primary",
        doc_type="incident",
    )
    assert [hit.doc_id for hit in filtered] == ["inc-2026-002"]

    # Pure JSON save & reload round-trip (zero pickle)
    save_path = tmp_path / "bm25_snapshot.json"
    index.save(save_path)
    raw_json = json.loads(save_path.read_text(encoding="utf-8"))
    assert raw_json["format_version"] == 1
    assert len(raw_json["chunks"]) == 3

    reloaded = BM25Index.load(save_path)
    assert reloaded.size == 3
    reloaded_hits = reloaded.search("40P01 deadlock_detected", top_k=5)
    assert len(reloaded_hits) == 1
    assert reloaded_hits[0].chunk_id == deadlock_hits[0].chunk_id
    assert math.isclose(reloaded_hits[0].score, deadlock_hits[0].score, rel_tol=1e-9)


def test_bm25_small_corpus_positive_scoring_and_empty_safety() -> None:
    single = _make_chunk(
        chunk_id="00000000-0000-5000-8000-000000000099",
        doc_id="rb-solo",
        doc_type="runbook",
        title="Kafka Lag Mitigation",
        service="kafka-broker",
        section_heading="Steps",
        text="Check consumer group lag on payment_events partition.",
    )
    index = BM25Index([single])
    hits = index.search("kafka consumer lag")
    assert len(hits) == 1
    assert hits[0].doc_id == "rb-solo"
    assert hits[0].score > 0.0
    assert index.search("completely_nonexistent_token_xyz") == []


# ---------------------------------------------------------------------------
# 2. Reciprocal Rank Fusion unit tests
# ---------------------------------------------------------------------------


def test_reciprocal_rank_fusion_exact_scores_and_tie_breaking() -> None:
    c_a = _make_retrieved("cid-a", doc_id="doc-a", score=0.90, source="dense")
    c_b = _make_retrieved("cid-b", doc_id="doc-b", score=0.95, source="dense")
    c_c = _make_retrieved("cid-c", doc_id="doc-c", score=0.70, source="dense")
    c_d = _make_retrieved("cid-d", doc_id="doc-d", score=4.20, source="bm25")

    dense_list = [c_b, c_a, c_c]  # ranks 1, 2, 3
    bm25_list = [c_a, c_d, c_b]  # ranks 1, 2, 3

    fused = reciprocal_rank_fusion([dense_list, bm25_list], top_k=4, rrf_k=60)
    assert [r.chunk_id for r in fused] == ["cid-a", "cid-b", "cid-d", "cid-c"]
    assert all(r.retrieval_source == "hybrid" for r in fused)

    expected = {
        "cid-a": (1.0 / 62) + (1.0 / 61),
        "cid-b": (1.0 / 61) + (1.0 / 63),
        "cid-d": 1.0 / 62,
        "cid-c": 1.0 / 63,
    }
    for hit in fused:
        assert math.isclose(hit.score, expected[hit.chunk_id], rel_tol=1e-12)

    # Exact tie-breaking: both chunks appear at rank 1 in one list (score 1/61 each).
    tie_z = _make_retrieved("cid-z", doc_id="doc-z", score=0.8, source="dense")
    tie_a = _make_retrieved("cid-a", doc_id="doc-a", score=3.1, source="bm25")
    tied = reciprocal_rank_fusion([[tie_z], [tie_a]], top_k=2, rrf_k=60)
    assert [r.chunk_id for r in tied] == ["cid-a", "cid-z"]
    assert math.isclose(tied[0].score, 1.0 / 61, rel_tol=1e-12)
    assert math.isclose(tied[1].score, 1.0 / 61, rel_tol=1e-12)


# ---------------------------------------------------------------------------
# 3. Live Qdrant integration tests (isolated test collection)
# ---------------------------------------------------------------------------


def test_qdrant_empty_collection_idempotent_upsert_and_filters(
    qdrant_test_store: QdrantVectorStore,
    tmp_path: Path,
) -> None:
    embedder = DeterministicHashEmbedder(dimension=384)
    # Searching before collection exists must return [] cleanly
    assert qdrant_test_store.search(embedder.embed_query("postgres"), top_k=5) == []

    docs = load_knowledge_base(SYNTHETIC_KB_DIR)
    chunks = MarkdownChunker(max_chars=1200, overlap_chars=150).chunk_documents(docs)
    assert len(docs) == 37
    assert len(chunks) == 143

    bm25_file = tmp_path / "bm25.json"
    retriever = HybridRetriever(
        embedder=embedder,
        vector_store=qdrant_test_store,
        bm25_path=bm25_file,
    )

    first_count = retriever.index_chunks(chunks)
    second_count = retriever.index_chunks(chunks)
    assert first_count == 143
    assert second_count == 143
    assert qdrant_test_store.count() == 143
    assert retriever.bm25_index.size == 143
    assert bm25_file.is_file()

    # Cosine search + payload filtering
    dense_hits = retriever.search(
        "postgres connection pool exhausted",
        top_k=5,
        mode="dense",
        service="postgres-primary",
        doc_type="runbook",
    )
    assert len(dense_hits) > 0
    for hit in dense_hits:
        assert hit.retrieval_source == "dense"
        assert hit.service == "postgres-primary"
        assert hit.doc_type == "runbook"
        assert hit.is_synthetic is True
        assert hit.doc_id
        assert hit.title
        assert hit.section_heading
        assert hit.source_path

    # Hybrid RRF search
    hybrid_hits = retriever.search(
        "postgres connection pool exhausted pgbouncer",
        top_k=5,
        mode="hybrid",
    )
    assert len(hybrid_hits) == 5
    assert all(hit.retrieval_source == "hybrid" for hit in hybrid_hits)
    assert hybrid_hits[0].score >= hybrid_hits[-1].score


# ---------------------------------------------------------------------------
# 4. Live all-MiniLM-L6-v2 CPU smoke test
# ---------------------------------------------------------------------------


def test_live_minilm_cpu_smoke_on_synthetic_kb(
    qdrant_test_store: QdrantVectorStore,
) -> None:
    embedder = SentenceTransformerEmbedder(
        model_name=get_settings().EMBEDDING_MODEL_NAME,
        device="cpu",
    )
    assert embedder.device == "cpu"

    docs = load_knowledge_base(SYNTHETIC_KB_DIR)
    chunks = MarkdownChunker(max_chars=1200, overlap_chars=150).chunk_documents(docs)

    retriever = HybridRetriever(embedder=embedder, vector_store=qdrant_test_store)
    indexed = retriever.index_chunks(chunks)
    assert indexed == len(chunks) == 143
    assert embedder.dimension == 384
    assert qdrant_test_store.count() == 143

    query = "database connection pool exhausted postgres"
    hybrid_hits = retriever.search(query, top_k=5, mode="hybrid")
    dense_hits = retriever.search(query, top_k=5, mode="dense")

    assert len(hybrid_hits) == 5
    assert len(dense_hits) == 5

    relevant_db_docs = {"rb-002", "inc-2026-002", "arch-004"}
    assert any(hit.doc_id in relevant_db_docs for hit in hybrid_hits[:3])
    assert any(hit.doc_id in relevant_db_docs for hit in dense_hits[:3])

    for hit in hybrid_hits:
        assert hit.doc_id
        assert hit.doc_type in {"runbook", "incident", "architecture", "deployment"}
        assert hit.title
        assert hit.service
        assert hit.section_heading
        assert hit.source_path.endswith(".md")
        assert hit.is_synthetic is True
        assert hit.score > 0.0
        assert hit.retrieval_source == "hybrid"


# ---------------------------------------------------------------------------
# 5. FastAPI endpoint tests
# ---------------------------------------------------------------------------


def test_knowledge_search_before_indexing_returns_empty(
    client: TestClient,
    test_retriever: HybridRetriever,
) -> None:
    response = client.post(
        "/api/v1/knowledge/search",
        json={"query": "database connection pool exhausted", "top_k": 5, "mode": "hybrid"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "database connection pool exhausted"
    assert body["mode"] == "hybrid"
    assert body["total"] == 0
    assert body["results"] == []


def test_knowledge_index_and_search_endpoints_all_modes(
    client: TestClient,
    test_retriever: HybridRetriever,
    qdrant_test_store: QdrantVectorStore,
) -> None:
    index_payload = {
        "source_dir": "data/synthetic/knowledge_base",
        "max_chars": 1200,
        "overlap_chars": 150,
    }
    res1 = client.post("/api/v1/knowledge/index", json=index_payload)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["source_dir"] == "data/synthetic/knowledge_base"
    assert data1["documents_indexed"] == 37
    assert data1["chunks_indexed"] == 143
    assert data1["collection"] == TEST_QDRANT_COLLECTION
    assert data1["embedding_model"] == "deterministic-hash-384"

    # Idempotent second index call
    res2 = client.post("/api/v1/knowledge/index", json=index_payload)
    assert res2.status_code == 200
    assert qdrant_test_store.count() == 143
    assert test_retriever.bm25_index.size == 143

    for mode in ("hybrid", "dense", "bm25"):
        search_res = client.post(
            "/api/v1/knowledge/search",
            json={
                "query": "postgres connection pool exhausted pgbouncer",
                "top_k": 5,
                "mode": mode,
                "service": "postgres-primary",
                "doc_type": "runbook",
            },
        )
        assert search_res.status_code == 200
        body = search_res.json()
        assert body["mode"] == mode
        assert body["total"] == len(body["results"])
        assert body["total"] >= 1
        top = body["results"][0]
        assert top["service"] == "postgres-primary"
        assert top["doc_type"] == "runbook"
        assert top["retrieval_source"] == mode
        assert top["is_synthetic"] is True
        for field in (
            "chunk_id",
            "doc_id",
            "doc_type",
            "title",
            "service",
            "section_heading",
            "chunk_index",
            "source_path",
            "text",
            "score",
        ):
            assert field in top


def test_knowledge_endpoints_validation_and_error_responses(
    client: TestClient,
    test_retriever: HybridRetriever,
) -> None:
    # Empty / whitespace query -> 422
    assert (
        client.post("/api/v1/knowledge/search", json={"query": "   "}).status_code == 422
    )
    # top_k out of bounds -> 422
    assert (
        client.post("/api/v1/knowledge/search", json={"query": "cpu", "top_k": 0}).status_code
        == 422
    )
    assert (
        client.post("/api/v1/knowledge/search", json={"query": "cpu", "top_k": 51}).status_code
        == 422
    )
    # Invalid mode -> 422
    assert (
        client.post(
            "/api/v1/knowledge/search",
            json={"query": "cpu", "mode": "unsupported"},
        ).status_code
        == 422
    )
    # overlap_chars >= max_chars -> 422
    assert (
        client.post(
            "/api/v1/knowledge/index",
            json={
                "source_dir": "data/synthetic/knowledge_base",
                "max_chars": 200,
                "overlap_chars": 200,
            },
        ).status_code
        == 422
    )
    # Path traversal outside repo -> 422
    assert (
        client.post("/api/v1/knowledge/index", json={"source_dir": "../outside"}).status_code
        == 422
    )
    # Missing directory inside repo -> 404
    assert (
        client.post(
            "/api/v1/knowledge/index",
            json={"source_dir": "data/synthetic/nonexistent_kb_dir"},
        ).status_code
        == 404
    )


def test_knowledge_endpoints_return_503_when_qdrant_unavailable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    test_retriever: HybridRetriever,
) -> None:
    def _raise_connect_error(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(test_retriever.vector_store, "upsert_chunks", _raise_connect_error)
    monkeypatch.setattr(test_retriever.vector_store, "search", _raise_connect_error)

    index_res = client.post(
        "/api/v1/knowledge/index",
        json={"source_dir": "data/synthetic/knowledge_base"},
    )
    assert index_res.status_code == 503

    search_res = client.post(
        "/api/v1/knowledge/search",
        json={"query": "postgres connection pool", "mode": "hybrid"},
    )
    assert search_res.status_code == 503
