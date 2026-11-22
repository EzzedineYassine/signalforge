import ast
import math
from pathlib import Path

import pytest

from evaluation.retrieval.metrics import (
    VALID_QUERY_CATEGORIES,
    BenchmarkQuery,
    citation_completeness,
    deduplicate_doc_ids,
    evaluate_retrieval_mode,
    load_benchmark_suite,
    mrr_at_k,
    ndcg_at_k,
    percentile,
    recall_at_k,
)
from services.retrieval.chunking.markdown_chunker import MarkdownChunker
from services.retrieval.ingestion.loader import load_knowledge_base
from services.retrieval.search.bm25_index import BM25Index
from services.retrieval.search.models import RetrievalMode, RetrievedChunk

REPO_ROOT = Path(__file__).resolve().parents[3]
SYNTHETIC_KB_DIR = REPO_ROOT / "data" / "synthetic" / "knowledge_base"
BENCHMARK_QUERIES_PATH = REPO_ROOT / "evaluation" / "retrieval" / "benchmark_queries.json"
EVALUATION_DIR = REPO_ROOT / "evaluation"
FORBIDDEN_IMPORTS = {"fastapi", "sqlalchemy", "psycopg", "alembic", "starlette", "pickle", "joblib"}


def _make_chunk(
    doc_id: str,
    chunk_index: int = 0,
    *,
    title: str = "Title",
    section_heading: str = "Overview",
    source_path: str = "runbooks/sample.md",
    is_synthetic: bool = True,
    score: float = 1.0,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"cid-{doc_id}-{chunk_index}",
        doc_id=doc_id,
        doc_type="runbook",
        title=title,
        service="postgres-primary",
        section_heading=section_heading,
        chunk_index=chunk_index,
        source_path=source_path,
        is_synthetic=is_synthetic,
        text="Operational guidance.",
        score=score,
        retrieval_source="bm25",
    )


class _BM25OnlyRetriever:
    """Fast local retriever adapter backed strictly by BM25Index for unit evaluation tests."""

    def __init__(self, bm25_index: BM25Index) -> None:
        self._bm25 = bm25_index

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        mode: RetrievalMode = "bm25",
        service: str | None = None,
        doc_type: str | None = None,
        rrf_k: int = 60,
    ) -> list[RetrievedChunk]:
        return self._bm25.search(query, top_k=top_k, service=service, doc_type=doc_type)


# ---------------------------------------------------------------------------
# 1. Hand-calculated metric unit tests & edge cases
# ---------------------------------------------------------------------------


def test_deduplicate_doc_ids_preserves_first_seen_order() -> None:
    chunks = [
        _make_chunk("rb-002", 0),
        _make_chunk("rb-002", 1),
        _make_chunk("inc-2026-002", 0),
        _make_chunk("rb-002", 2),
        _make_chunk("arch-004", 0),
        _make_chunk("inc-2026-002", 1),
    ]
    assert deduplicate_doc_ids(chunks) == ["rb-002", "inc-2026-002", "arch-004"]
    assert deduplicate_doc_ids([]) == []


def test_hand_calculated_recall_mrr_and_ndcg() -> None:
    # Ranked doc list: ["d0", "d2", "d1", "d4", "d3"]
    # Relevant docs:   ["d1", "d3"]
    retrieved = ["d0", "d2", "d1", "d4", "d3"]
    relevant = ["d1", "d3"]

    # Recall@5: both d1 and d3 in top 5 -> 2/2 = 1.0; Recall@3: only d1 in top 3 -> 1/2 = 0.5
    assert math.isclose(recall_at_k(retrieved, relevant, k=5), 1.0, rel_tol=1e-12)
    assert math.isclose(recall_at_k(retrieved, relevant, k=3), 0.5, rel_tol=1e-12)
    assert math.isclose(recall_at_k(retrieved, relevant, k=2), 0.0, rel_tol=1e-12)

    # MRR@10: first relevant doc ("d1") is at rank 3 -> 1/3
    assert math.isclose(mrr_at_k(retrieved, relevant, k=10), 1.0 / 3.0, rel_tol=1e-12)
    assert math.isclose(mrr_at_k(retrieved, relevant, k=2), 0.0, rel_tol=1e-12)

    # nDCG@10: hits at rank 3 and rank 5
    expected_dcg = (1.0 / math.log2(3 + 1)) + (1.0 / math.log2(5 + 1))
    expected_idcg = (1.0 / math.log2(1 + 1)) + (1.0 / math.log2(2 + 1))
    assert math.isclose(
        ndcg_at_k(retrieved, relevant, k=10),
        expected_dcg / expected_idcg,
        rel_tol=1e-12,
    )

    # Perfect ranking yields 1.0 across all metrics
    assert math.isclose(recall_at_k(["d1", "d3"], relevant, k=5), 1.0, rel_tol=1e-12)
    assert math.isclose(mrr_at_k(["d1", "d3"], relevant, k=10), 1.0, rel_tol=1e-12)
    assert math.isclose(ndcg_at_k(["d1", "d3"], relevant, k=10), 1.0, rel_tol=1e-12)

    # No hits -> 0.0
    assert recall_at_k(["x", "y"], relevant, k=5) == 0.0
    assert mrr_at_k(["x", "y"], relevant, k=10) == 0.0
    assert ndcg_at_k(["x", "y"], relevant, k=10) == 0.0

    # Duplicate doc_ids in input must never inflate Recall or nDCG above 1.0
    assert math.isclose(recall_at_k(["d1", "d1", "d1"], relevant, k=5), 0.5, rel_tol=1e-12)
    single_hit_ndcg = (1.0 / math.log2(2)) / expected_idcg
    assert math.isclose(
        ndcg_at_k(["d1", "d1", "d1"], relevant, k=10),
        single_hit_ndcg,
        rel_tol=1e-12,
    )


def test_citation_completeness_and_percentile() -> None:
    complete = _make_chunk("rb-001", 0)
    missing_heading = _make_chunk("rb-002", 0, section_heading="   ")
    assert citation_completeness([complete]) == 1.0
    assert math.isclose(citation_completeness([complete, missing_heading]), 0.5, rel_tol=1e-12)
    assert citation_completeness([]) == 0.0

    assert math.isclose(percentile([10.0, 20.0, 30.0, 40.0], 50.0), 25.0, rel_tol=1e-12)
    assert math.isclose(percentile([10.0, 20.0, 30.0, 40.0], 95.0), 38.5, rel_tol=1e-12)
    assert percentile([], 50.0) == 0.0


def test_metric_validation_errors() -> None:
    with pytest.raises(ValueError, match="k must be a positive integer"):
        recall_at_k(["a"], ["a"], k=0)
    with pytest.raises(ValueError, match="k must be a positive integer"):
        mrr_at_k(["a"], ["a"], k=-1)
    with pytest.raises(ValueError, match="k must be a positive integer"):
        ndcg_at_k(["a"], ["a"], k=0)
    with pytest.raises(ValueError, match="relevant_docs"):
        recall_at_k(["a"], [], k=5)
    with pytest.raises(ValueError, match="relevant_docs"):
        mrr_at_k(["a"], [], k=10)
    with pytest.raises(ValueError, match="relevant_docs"):
        ndcg_at_k(["a"], [], k=10)
    with pytest.raises(ValueError, match="pct"):
        percentile([1.0], 101.0)


# ---------------------------------------------------------------------------
# 2. Ground-truth benchmark integrity test
# ---------------------------------------------------------------------------


def test_benchmark_queries_ground_truth_integrity() -> None:
    suite = load_benchmark_suite(BENCHMARK_QUERIES_PATH)
    assert suite.is_synthetic_corpus is True
    assert len(suite.queries) == 24

    corpus_docs = load_knowledge_base(SYNTHETIC_KB_DIR)
    corpus_doc_ids = {doc.doc_id for doc in corpus_docs}
    assert len(corpus_doc_ids) == 37

    seen_query_ids: set[str] = set()
    categories_present: set[str] = set()

    for item in suite.queries:
        assert item.query_id not in seen_query_ids
        seen_query_ids.add(item.query_id)
        assert item.query.strip()
        assert item.category in VALID_QUERY_CATEGORIES
        categories_present.add(item.category)
        assert len(item.expected_doc_ids) >= 1
        assert len(item.expected_doc_ids) == len(set(item.expected_doc_ids))
        missing = [doc_id for doc_id in item.expected_doc_ids if doc_id not in corpus_doc_ids]
        assert missing == [], f"{item.query_id} references unknown doc_ids: {missing}"

    assert categories_present == set(VALID_QUERY_CATEGORIES)


# ---------------------------------------------------------------------------
# 3. Fast evaluation runner integration test & import boundary check
# ---------------------------------------------------------------------------


def test_evaluate_retrieval_mode_bm25_fast_integration() -> None:
    docs = load_knowledge_base(SYNTHETIC_KB_DIR)
    chunks = MarkdownChunker(max_chars=1200, overlap_chars=150).chunk_documents(docs)
    bm25_index = BM25Index(chunks)
    retriever = _BM25OnlyRetriever(bm25_index)
    suite = load_benchmark_suite(BENCHMARK_QUERIES_PATH)

    summary = evaluate_retrieval_mode(retriever, suite.queries, mode="bm25", top_k=10)
    assert summary.mode == "bm25"
    assert summary.num_queries == 24
    assert summary.top_k == 10
    assert len(summary.per_query) == 24
    assert set(summary.per_category.keys()) == set(VALID_QUERY_CATEGORIES)

    for metric in (
        summary.recall_at_5,
        summary.mrr_at_10,
        summary.ndcg_at_10,
        summary.citation_completeness,
    ):
        assert math.isfinite(metric)
        assert 0.0 <= metric <= 1.0

    assert summary.p50_latency_ms >= 0.0
    assert summary.p95_latency_ms >= summary.p50_latency_ms

    # Validation checks on evaluate_retrieval_mode
    with pytest.raises(ValueError, match="queries cannot be empty"):
        evaluate_retrieval_mode(retriever, [], mode="bm25")
    sample_q = BenchmarkQuery(
        query_id="q-test",
        category="exact_lookup",
        query="postgres",
        expected_doc_ids=["rb-002"],
    )
    with pytest.raises(ValueError, match="top_k"):
        evaluate_retrieval_mode(retriever, [sample_q], mode="bm25", top_k=0)


def test_evaluation_package_has_no_framework_or_db_imports() -> None:
    py_files = sorted(EVALUATION_DIR.rglob("*.py"))
    assert py_files
    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_pkg = alias.name.split(".")[0]
                    assert top_pkg not in FORBIDDEN_IMPORTS, f"{py_file} imports {top_pkg}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_pkg = node.module.split(".")[0]
                assert top_pkg not in FORBIDDEN_IMPORTS, f"{py_file} imports {top_pkg}"
