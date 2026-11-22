import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from apps.api.src.core.config import get_settings
from evaluation.retrieval.metrics import (
    evaluate_retrieval_mode,
    load_benchmark_suite,
)
from services.retrieval.chunking.markdown_chunker import MarkdownChunker
from services.retrieval.embeddings.embedder import SentenceTransformerEmbedder
from services.retrieval.ingestion.loader import load_knowledge_base
from services.retrieval.search.bm25_index import BM25Index
from services.retrieval.search.hybrid import HybridRetriever
from services.retrieval.search.models import RetrievalMode
from services.retrieval.search.qdrant_store import QdrantVectorStore

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB_DIR = REPO_ROOT / "data" / "synthetic" / "knowledge_base"
DEFAULT_QUERIES_PATH = REPO_ROOT / "evaluation" / "retrieval" / "benchmark_queries.json"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "experiments" / "benchmarks" / "retrieval_kb" / "results.json"
BENCHMARK_COLLECTION = "signalforge_knowledge_bench"
MODES: tuple[RetrievalMode, ...] = ("bm25", "dense", "hybrid")


def run_retrieval_benchmark(
    *,
    kb_dir: Path = DEFAULT_KB_DIR,
    queries_path: Path = DEFAULT_QUERIES_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    collection_name: str = BENCHMARK_COLLECTION,
    qdrant_url: str | None = None,
    embedding_model: str | None = None,
    max_chars: int = 1200,
    overlap_chars: int = 150,
    top_k: int = 10,
    rrf_k: int = 60,
) -> dict[str, Any]:
    """Run reproducible BM25 vs. Dense vs. Hybrid retrieval benchmark against live Qdrant."""
    settings = get_settings()
    resolved_url = qdrant_url or settings.QDRANT_URL
    resolved_model = embedding_model or settings.EMBEDDING_MODEL_NAME

    suite = load_benchmark_suite(queries_path)
    documents = load_knowledge_base(kb_dir)
    corpus_doc_ids = {doc.doc_id for doc in documents}

    missing_refs = sorted(
        {
            doc_id
            for q in suite.queries
            for doc_id in q.expected_doc_ids
            if doc_id not in corpus_doc_ids
        }
    )
    if missing_refs:
        raise ValueError(
            f"Benchmark query ground truth references missing doc_ids: {missing_refs}"
        )

    chunker = MarkdownChunker(max_chars=max_chars, overlap_chars=overlap_chars)
    chunks = chunker.chunk_documents(documents)

    embedder = SentenceTransformerEmbedder(model_name=resolved_model, device="cpu")
    vector_store = QdrantVectorStore(url=resolved_url, collection_name=collection_name)
    bm25_index = BM25Index()
    retriever = HybridRetriever(
        embedder=embedder,
        vector_store=vector_store,
        bm25_index=bm25_index,
    )

    vector_store.delete_collection()
    try:
        t_idx = time.perf_counter()
        chunks_indexed = retriever.index_chunks(chunks)
        index_seconds = time.perf_counter() - t_idx

        # Single untimed warmup query so Qdrant HNSW/connection pools are warm before timing.
        retriever.search(suite.queries[0].query, top_k=top_k, mode="hybrid", rrf_k=rrf_k)

        mode_results: dict[str, Any] = {}
        for mode in MODES:
            summary = evaluate_retrieval_mode(
                retriever,
                suite.queries,
                mode=mode,
                top_k=top_k,
                recall_k=5,
                mrr_k=10,
                ndcg_k=10,
                rrf_k=rrf_k,
            )
            mode_results[mode] = summary.model_dump()
    finally:
        vector_store.delete_collection()

    payload: dict[str, Any] = {
        "is_synthetic_corpus": suite.is_synthetic_corpus,
        "corpus_source": suite.corpus_source,
        "queries_file": queries_path.relative_to(REPO_ROOT).as_posix()
        if queries_path.is_relative_to(REPO_ROOT)
        else queries_path.as_posix(),
        "documents_indexed": len(documents),
        "chunks_indexed": chunks_indexed,
        "chunking": {
            "max_chars": max_chars,
            "overlap_chars": overlap_chars,
        },
        "embedding_model": resolved_model,
        "embedding_dimension": embedder.dimension,
        "device": embedder.device,
        "qdrant_collection_temporary": collection_name,
        "top_k": top_k,
        "rrf_k": rrf_k,
        "num_queries": len(suite.queries),
        "index_seconds": round(index_seconds, 4),
        "modes": mode_results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run BM25 vs. Dense vs. Hybrid retrieval benchmark against live Qdrant."
    )
    parser.add_argument("--kb-dir", type=Path, default=DEFAULT_KB_DIR)
    parser.add_argument("--queries-path", type=Path, default=DEFAULT_QUERIES_PATH)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--collection", type=str, default=BENCHMARK_COLLECTION)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--rrf-k", type=int, default=60)
    return parser


def _print_summary_table(payload: dict[str, Any]) -> None:
    print(
        f"Indexed {payload['documents_indexed']} docs ({payload['chunks_indexed']} chunks) "
        f"in {payload['index_seconds']:.2f}s using {payload['embedding_model']} "
        f"({payload['device']}, dim={payload['embedding_dimension']}).\n"
    )
    header = (
        f"{'Mode':<8} | {'Recall@5':>8} | {'MRR@10':>8} | {'nDCG@10':>8} | "
        f"{'Citation':>8} | {'p50 (ms)':>8} | {'p95 (ms)':>8}"
    )
    print(header)
    print("-" * len(header))
    for mode in MODES:
        m = payload["modes"][mode]
        print(
            f"{mode:<8} | {m['recall_at_5']:8.4f} | {m['mrr_at_10']:8.4f} | "
            f"{m['ndcg_at_10']:8.4f} | {m['citation_completeness']:8.4f} | "
            f"{m['p50_latency_ms']:8.2f} | {m['p95_latency_ms']:8.2f}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        payload = run_retrieval_benchmark(
            kb_dir=args.kb_dir,
            queries_path=args.queries_path,
            output_path=args.output_path,
            collection_name=args.collection,
            top_k=args.top_k,
            rrf_k=args.rrf_k,
        )
    except Exception as exc:
        print(f"Benchmark failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1

    _print_summary_table(payload)
    print(f"\nSaved results to: {args.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
