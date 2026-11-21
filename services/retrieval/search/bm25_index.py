import json
import re
from collections.abc import Sequence
from pathlib import Path

from rank_bm25 import BM25Okapi

from services.retrieval.chunking.markdown_chunker import DocumentChunk
from services.retrieval.search.models import RetrievedChunk

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")


def tokenize_for_bm25(text: str) -> list[str]:
    """Tokenize text preserving compound identifiers (error codes, services) and sub-terms."""
    raw_tokens = _TOKEN_RE.findall(text.lower())
    tokens: list[str] = []
    for token in raw_tokens:
        tokens.append(token)
        if "-" in token or "_" in token:
            tokens.extend(part for part in re.split(r"[-_]", token) if part)
    return tokens


class BM25Index:
    """In-memory BM25Okapi keyword index with pure JSON persistence (no pickle)."""

    def __init__(self, chunks: Sequence[DocumentChunk] | None = None) -> None:
        self._chunks_by_id: dict[str, DocumentChunk] = {}
        self._chunks: list[DocumentChunk] = []
        self._bm25: BM25Okapi | None = None
        if chunks:
            self.upsert_chunks(chunks)

    @property
    def size(self) -> int:
        return len(self._chunks)

    def _rebuild(self) -> None:
        self._chunks = sorted(
            self._chunks_by_id.values(),
            key=lambda c: (c.doc_id, c.chunk_index, c.chunk_id),
        )
        if not self._chunks:
            self._bm25 = None
            return
        corpus_tokens = [
            tokenize_for_bm25(f"{c.title} {c.service} {c.section_heading} {c.text}")
            for c in self._chunks
        ]
        bm25 = BM25Okapi(corpus_tokens)
        # Ensure positive IDF floor so tiny corpora (1-2 chunks) or terms in exactly N/2 chunks
        # still receive positive BM25 scores when matched while non-matches stay 0.0.
        positive_floor = max(bm25.epsilon * max(bm25.average_idf, 0.0), 1e-6)
        for token, idf_val in bm25.idf.items():
            if idf_val <= 0.0:
                bm25.idf[token] = positive_floor
        self._bm25 = bm25

    def upsert_chunks(self, chunks: Sequence[DocumentChunk]) -> int:
        """Insert or update chunks by deterministic chunk_id and rebuild the BM25 index."""
        for chunk in chunks:
            self._chunks_by_id[chunk.chunk_id] = chunk
        self._rebuild()
        return len(self._chunks)

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        service: str | None = None,
        doc_type: str | None = None,
    ) -> list[RetrievedChunk]:
        """Search indexed chunks by BM25 score with optional service and doc_type filters."""
        if top_k <= 0 or self._bm25 is None or not self._chunks:
            return []

        query_tokens = tokenize_for_bm25(query)
        if not query_tokens:
            return []

        raw_scores = self._bm25.get_scores(query_tokens)
        norm_service = service.strip().lower() if service is not None else None
        norm_doc_type = doc_type.strip().lower() if doc_type is not None else None

        scored: list[tuple[float, DocumentChunk]] = []
        for chunk, raw_score in zip(self._chunks, raw_scores, strict=True):
            score = float(raw_score)
            if score <= 0.0:
                continue
            if norm_service is not None and chunk.service.lower() != norm_service:
                continue
            if norm_doc_type is not None and chunk.doc_type.lower() != norm_doc_type:
                continue
            scored.append((score, chunk))

        scored.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return [
            RetrievedChunk(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                doc_type=chunk.doc_type,
                title=chunk.title,
                service=chunk.service,
                section_heading=chunk.section_heading,
                chunk_index=chunk.chunk_index,
                source_path=chunk.source_path,
                is_synthetic=chunk.is_synthetic,
                text=chunk.text,
                score=score,
                retrieval_source="bm25",
            )
            for score, chunk in scored[:top_k]
        ]

    def save(self, path: Path) -> None:
        """Persist indexed chunks to pure JSON so BM25 can be rebuilt safely on load."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": 1,
            "chunks": [chunk.model_dump() for chunk in self._chunks],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        """Load chunks from JSON and rebuild BM25Okapi cleanly without pickle."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        chunks = [DocumentChunk.model_validate(item) for item in payload.get("chunks", [])]
        return cls(chunks)
