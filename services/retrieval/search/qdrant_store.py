from collections.abc import Sequence

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from services.retrieval.chunking.markdown_chunker import DocumentChunk
from services.retrieval.search.models import RetrievedChunk


class QdrantVectorStore:
    """Qdrant vector store wrapper using qdrant-client query_points API."""

    def __init__(
        self,
        url: str = "http://localhost:6333",
        collection_name: str = "signalforge_knowledge",
        timeout: float = 10.0,
        client: QdrantClient | None = None,
    ) -> None:
        self.url = url
        self.collection_name = collection_name
        self._client = client if client is not None else QdrantClient(url=url, timeout=timeout)

    def ensure_collection(self, vector_size: int) -> None:
        if vector_size < 1:
            raise ValueError("vector_size must be at least 1")
        if not self._client.collection_exists(self.collection_name):
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qmodels.VectorParams(
                    size=vector_size,
                    distance=qmodels.Distance.COSINE,
                ),
            )

    def upsert_chunks(
        self,
        chunks: Sequence[DocumentChunk],
        vectors: Sequence[Sequence[float]],
        *,
        batch_size: int = 128,
    ) -> int:
        """Idempotently upsert chunks keyed by their deterministic UUID5 chunk_id."""
        if len(chunks) != len(vectors):
            raise ValueError(
                f"Length mismatch: {len(chunks)} chunks vs {len(vectors)} embedding vectors"
            )
        if not chunks:
            return 0

        self.ensure_collection(len(vectors[0]))

        points = [
            qmodels.PointStruct(
                id=chunk.chunk_id,
                vector=[float(v) for v in vec],
                payload=chunk.model_dump(),
            )
            for chunk, vec in zip(chunks, vectors, strict=True)
        ]

        for start in range(0, len(points), batch_size):
            self._client.upsert(
                collection_name=self.collection_name,
                points=points[start : start + batch_size],
                wait=True,
            )
        return len(points)

    def count(self) -> int:
        if not self._client.collection_exists(self.collection_name):
            return 0
        return int(self._client.count(collection_name=self.collection_name, exact=True).count)

    def delete_collection(self) -> None:
        if self._client.collection_exists(self.collection_name):
            self._client.delete_collection(collection_name=self.collection_name)

    def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
        *,
        service: str | None = None,
        doc_type: str | None = None,
    ) -> list[RetrievedChunk]:
        """Run cosine similarity search with optional service and doc_type payload filters."""
        if top_k <= 0 or not self._client.collection_exists(self.collection_name):
            return []

        conditions: list[qmodels.Condition] = []
        if service is not None:
            conditions.append(
                qmodels.FieldCondition(
                    key="service",
                    match=qmodels.MatchValue(value=service.strip().lower()),
                )
            )
        if doc_type is not None:
            conditions.append(
                qmodels.FieldCondition(
                    key="doc_type",
                    match=qmodels.MatchValue(value=doc_type.strip().lower()),
                )
            )

        query_filter = qmodels.Filter(must=conditions) if conditions else None
        response = self._client.query_points(
            collection_name=self.collection_name,
            query=[float(v) for v in query_vector],
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
        )

        results: list[RetrievedChunk] = []
        for point in response.points:
            payload = point.payload or {}
            results.append(
                RetrievedChunk(
                    chunk_id=str(payload.get("chunk_id", point.id)),
                    doc_id=str(payload["doc_id"]),
                    doc_type=payload["doc_type"],
                    title=str(payload["title"]),
                    service=str(payload["service"]),
                    section_heading=str(payload["section_heading"]),
                    chunk_index=int(payload["chunk_index"]),
                    source_path=str(payload["source_path"]),
                    is_synthetic=bool(payload.get("is_synthetic", False)),
                    text=str(payload["text"]),
                    score=float(point.score),
                    retrieval_source="dense",
                )
            )

        results.sort(key=lambda r: (-r.score, r.chunk_id))
        return results
