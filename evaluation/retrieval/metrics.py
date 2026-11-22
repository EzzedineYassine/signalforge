import json
import math
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from services.retrieval.search.models import RetrievalMode, RetrievedChunk

QueryCategory = Literal["exact_lookup", "conceptual_symptom", "multi_service_incident"]
VALID_QUERY_CATEGORIES: tuple[str, ...] = (
    "exact_lookup",
    "conceptual_symptom",
    "multi_service_incident",
)


class _SearchableRetriever(Protocol):
    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        mode: RetrievalMode = "hybrid",
        service: str | None = None,
        doc_type: str | None = None,
        rrf_k: int = 60,
    ) -> list[RetrievedChunk]: ...


class BenchmarkQuery(BaseModel):
    query_id: str
    category: QueryCategory
    query: str
    expected_doc_ids: list[str] = Field(min_length=1)
    service_filter: str | None = None
    doc_type_filter: str | None = None


class BenchmarkSuite(BaseModel):
    is_synthetic_corpus: bool
    corpus_source: str
    description: str
    queries: list[BenchmarkQuery] = Field(min_length=1)


class QueryEvaluationResult(BaseModel):
    query_id: str
    category: QueryCategory
    query: str
    expected_doc_ids: list[str]
    retrieved_doc_ids: list[str]
    recall_at_5: float
    mrr_at_10: float
    ndcg_at_10: float
    citation_completeness: float
    latency_ms: float


class RetrievalModeSummary(BaseModel):
    mode: RetrievalMode
    num_queries: int
    top_k: int
    recall_at_5: float
    mrr_at_10: float
    ndcg_at_10: float
    citation_completeness: float
    p50_latency_ms: float
    p95_latency_ms: float
    per_category: dict[str, dict[str, float]]
    per_query: list[QueryEvaluationResult]


def load_benchmark_suite(path: Path) -> BenchmarkSuite:
    """Load and validate a retrieval benchmark query suite from JSON."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return BenchmarkSuite.model_validate(raw)


def deduplicate_doc_ids(chunks: Sequence[RetrievedChunk]) -> list[str]:
    """Return unique doc_ids in first-seen rank order so multi-chunk docs never inflate scores."""
    seen: set[str] = set()
    ordered: list[str] = []
    for chunk in chunks:
        if chunk.doc_id not in seen:
            seen.add(chunk.doc_id)
            ordered.append(chunk.doc_id)
    return ordered


def _validate_eval_args(relevant_docs: Sequence[str], k: int) -> set[str]:
    if k <= 0:
        raise ValueError(f"k must be a positive integer, got {k}")
    relevant_set = {doc_id for doc_id in relevant_docs if doc_id}
    if not relevant_set:
        raise ValueError("relevant_docs must contain at least one non-empty doc_id")
    return relevant_set


def recall_at_k(
    retrieved_docs: Sequence[str],
    relevant_docs: Sequence[str],
    k: int = 5,
) -> float:
    """Compute document-level Recall@k."""
    relevant_set = _validate_eval_args(relevant_docs, k)
    hits = set(retrieved_docs[:k]) & relevant_set
    return len(hits) / len(relevant_set)


def mrr_at_k(
    retrieved_docs: Sequence[str],
    relevant_docs: Sequence[str],
    k: int = 10,
) -> float:
    """Compute document-level Mean Reciprocal Rank@k (1 / rank of first relevant hit)."""
    relevant_set = _validate_eval_args(relevant_docs, k)
    for rank, doc_id in enumerate(retrieved_docs[:k], start=1):
        if doc_id in relevant_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(
    retrieved_docs: Sequence[str],
    relevant_docs: Sequence[str],
    k: int = 10,
) -> float:
    """Compute binary-relevance document-level nDCG@k using 1 / log2(rank + 1)."""
    relevant_set = _validate_eval_args(relevant_docs, k)

    dcg = 0.0
    seen: set[str] = set()
    for rank, doc_id in enumerate(retrieved_docs[:k], start=1):
        if doc_id in relevant_set and doc_id not in seen:
            dcg += 1.0 / math.log2(rank + 1)
            seen.add(doc_id)

    ideal_hits = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0.0 else 0.0


def citation_completeness(chunks: Sequence[RetrievedChunk]) -> float:
    """Fraction of retrieved chunks carrying complete, non-empty citation metadata."""
    if not chunks:
        return 0.0

    complete = 0
    for chunk in chunks:
        required_strings = (
            chunk.doc_id,
            chunk.doc_type,
            chunk.title,
            chunk.section_heading,
            chunk.source_path,
        )
        strings_ok = all(isinstance(val, str) and bool(val.strip()) for val in required_strings)
        flag_ok = isinstance(chunk.is_synthetic, bool)
        if strings_ok and flag_ok:
            complete += 1
    return complete / len(chunks)


def percentile(values: Sequence[float], pct: float) -> float:
    """Compute linear-interpolation percentile (0 <= pct <= 100) using stdlib only."""
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"pct must be between 0 and 100, got {pct}")
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def evaluate_retrieval_mode(
    retriever: _SearchableRetriever,
    queries: Sequence[BenchmarkQuery],
    *,
    mode: RetrievalMode,
    top_k: int = 10,
    recall_k: int = 5,
    mrr_k: int = 10,
    ndcg_k: int = 10,
    rrf_k: int = 60,
) -> RetrievalModeSummary:
    """Evaluate a retriever in a single mode across all benchmark queries."""
    if not queries:
        raise ValueError("queries cannot be empty")
    if top_k <= 0:
        raise ValueError(f"top_k must be a positive integer, got {top_k}")
    if recall_k <= 0 or mrr_k <= 0 or ndcg_k <= 0:
        raise ValueError("recall_k, mrr_k, and ndcg_k must all be positive integers")

    per_query: list[QueryEvaluationResult] = []
    latencies: list[float] = []

    for item in queries:
        t0 = time.perf_counter()
        chunks = retriever.search(
            item.query,
            top_k=top_k,
            mode=mode,
            service=item.service_filter,
            doc_type=item.doc_type_filter,
            rrf_k=rrf_k,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(latency_ms)

        ranked_docs = deduplicate_doc_ids(chunks)
        r_at_5 = recall_at_k(ranked_docs, item.expected_doc_ids, k=recall_k)
        mrr = mrr_at_k(ranked_docs, item.expected_doc_ids, k=mrr_k)
        ndcg = ndcg_at_k(ranked_docs, item.expected_doc_ids, k=ndcg_k)
        cit = citation_completeness(chunks)

        per_query.append(
            QueryEvaluationResult(
                query_id=item.query_id,
                category=item.category,
                query=item.query,
                expected_doc_ids=list(item.expected_doc_ids),
                retrieved_doc_ids=ranked_docs,
                recall_at_5=round(r_at_5, 6),
                mrr_at_10=round(mrr, 6),
                ndcg_at_10=round(ndcg, 6),
                citation_completeness=round(cit, 6),
                latency_ms=round(latency_ms, 4),
            )
        )

    n = len(per_query)
    per_category: dict[str, dict[str, float]] = {}
    for category in sorted({row.category for row in per_query}):
        cat_rows = [row for row in per_query if row.category == category]
        cat_n = len(cat_rows)
        per_category[category] = {
            "num_queries": float(cat_n),
            "recall_at_5": round(sum(r.recall_at_5 for r in cat_rows) / cat_n, 6),
            "mrr_at_10": round(sum(r.mrr_at_10 for r in cat_rows) / cat_n, 6),
            "ndcg_at_10": round(sum(r.ndcg_at_10 for r in cat_rows) / cat_n, 6),
        }

    return RetrievalModeSummary(
        mode=mode,
        num_queries=n,
        top_k=top_k,
        recall_at_5=round(sum(r.recall_at_5 for r in per_query) / n, 6),
        mrr_at_10=round(sum(r.mrr_at_10 for r in per_query) / n, 6),
        ndcg_at_10=round(sum(r.ndcg_at_10 for r in per_query) / n, 6),
        citation_completeness=round(sum(r.citation_completeness for r in per_query) / n, 6),
        p50_latency_ms=round(percentile(latencies, 50.0), 4),
        p95_latency_ms=round(percentile(latencies, 95.0), 4),
        per_category=per_category,
        per_query=per_query,
    )
