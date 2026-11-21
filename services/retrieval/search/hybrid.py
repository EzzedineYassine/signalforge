from collections.abc import Sequence
from pathlib import Path

from services.retrieval.chunking.markdown_chunker import DocumentChunk
from services.retrieval.embeddings.embedder import EmbeddingProvider
from services.retrieval.search.bm25_index import BM25Index
from services.retrieval.search.models import RetrievalMode, RetrievedChunk
from services.retrieval.search.qdrant_store import QdrantVectorStore


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[RetrievedChunk]],
    *,
    top_k: int = 5,
    rrf_k: int = 60,
) -> list[RetrievedChunk]:
    """Fuse multiple ranked chunk lists via Reciprocal Rank Fusion (1 / (rrf_k + rank)).

    Ties are broken deterministically by ascending `chunk_id`.
    """
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if rrf_k < 1:
        raise ValueError("rrf_k must be at least 1")

    fused_scores: dict[str, float] = {}
    chunk_by_id: dict[str, RetrievedChunk] = {}

    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked, start=1):
            fused_scores[chunk.chunk_id] = fused_scores.get(chunk.chunk_id, 0.0) + (
                1.0 / (rrf_k + rank)
            )
            chunk_by_id.setdefault(chunk.chunk_id, chunk)

    ordered_ids = sorted(fused_scores, key=lambda cid: (-fused_scores[cid], cid))
    return [
        chunk_by_id[cid].model_copy(
            update={"score": float(fused_scores[cid]), "retrieval_source": "hybrid"}
        )
        for cid in ordered_ids[:top_k]
    ]


class HybridRetriever:
    """Combines dense Qdrant cosine search and lexical BM25Okapi via Reciprocal Rank Fusion."""

    def __init__(
        self,
        embedder: EmbeddingProvider,
        vector_store: QdrantVectorStore,
        bm25_index: BM25Index | None = None,
        bm25_path: Path | None = None,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.bm25_path = bm25_path
        if bm25_index is not None:
            self.bm25_index = bm25_index
        elif bm25_path is not None and bm25_path.is_file():
            self.bm25_index = BM25Index.load(bm25_path)
        else:
            self.bm25_index = BM25Index()

    @staticmethod
    def _format_chunk_for_embedding(chunk: DocumentChunk) -> str:
        return (
            f"{chunk.title}\nService: {chunk.service}\n"
            f"Section: {chunk.section_heading}\n{chunk.text}"
        )

    def index_chunks(self, chunks: Sequence[DocumentChunk]) -> int:
        """Embed and idempotently index chunks into both Qdrant and BM25."""
        if not chunks:
            return 0
        texts = [self._format_chunk_for_embedding(chunk) for chunk in chunks]
        vectors = self.embedder.embed_texts(texts)
        self.vector_store.upsert_chunks(chunks, vectors)
        self.bm25_index.upsert_chunks(chunks)
        if self.bm25_path is not None:
            self.bm25_index.save(self.bm25_path)
        return len(chunks)

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        mode: RetrievalMode = "hybrid",
        service: str | None = None,
        doc_type: str | None = None,
        rrf_k: int = 60,
    ) -> list[RetrievedChunk]:
        """Run dense, BM25, or hybrid RRF search with optional metadata filters."""
        cleaned_query = query.strip()
        if not cleaned_query:
            raise ValueError("query cannot be empty")
        if top_k < 1:
            raise ValueError("top_k must be at least 1")

        if mode == "bm25":
            return self.bm25_index.search(
                cleaned_query, top_k=top_k, service=service, doc_type=doc_type
            )

        if mode == "dense":
            query_vector = self.embedder.embed_query(cleaned_query)
            return self.vector_store.search(
                query_vector, top_k=top_k, service=service, doc_type=doc_type
            )

        if mode == "hybrid":
            candidate_k = max(top_k * 4, 20)
            query_vector = self.embedder.embed_query(cleaned_query)
            dense_hits = self.vector_store.search(
                query_vector, top_k=candidate_k, service=service, doc_type=doc_type
            )
            bm25_hits = self.bm25_index.search(
                cleaned_query, top_k=candidate_k, service=service, doc_type=doc_type
            )
            return reciprocal_rank_fusion([dense_hits, bm25_hits], top_k=top_k, rrf_k=rrf_k)

        raise ValueError(f"Unsupported retrieval mode: {mode!r}")
