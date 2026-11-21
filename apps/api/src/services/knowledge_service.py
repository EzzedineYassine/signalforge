import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx
from qdrant_client.http.exceptions import (
    ApiException,
    ResponseHandlingException,
    UnexpectedResponse,
)

from apps.api.src.core.config import get_settings
from services.retrieval.chunking.markdown_chunker import MarkdownChunker
from services.retrieval.embeddings.embedder import SentenceTransformerEmbedder
from services.retrieval.ingestion.loader import DocType, load_knowledge_base
from services.retrieval.search.hybrid import HybridRetriever
from services.retrieval.search.models import RetrievalMode, RetrievedChunk
from services.retrieval.search.qdrant_store import QdrantVectorStore

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_KNOWLEDGE_DIR = "data/synthetic/knowledge_base"
DEFAULT_BM25_INDEX_PATH = REPO_ROOT / "data" / "indexes" / "bm25_index.json"

_QDRANT_TRANSPORT_ERRORS = (
    ApiException,
    ResponseHandlingException,
    UnexpectedResponse,
    httpx.HTTPError,
    OSError,
    TimeoutError,
)


class KnowledgeSourceNotFoundError(FileNotFoundError):
    """Raised when a requested knowledge base directory or its Markdown files are missing."""


class KnowledgeValidationError(ValueError):
    """Raised when knowledge indexing/search inputs fail safety or domain validation."""


class QdrantUnavailableError(RuntimeError):
    """Raised when Qdrant cannot be reached or returns an unexpected server/transport error."""


@dataclass(frozen=True)
class KnowledgeIndexResult:
    source_dir: str
    documents_indexed: int
    chunks_indexed: int
    collection: str
    embedding_model: str


def resolve_knowledge_source_dir(
    source_dir: str,
    *,
    repo_root: Path = REPO_ROOT,
) -> tuple[Path, str]:
    """Validate and resolve a knowledge source directory safely within the repository root."""
    cleaned = source_dir.strip()
    if not cleaned:
        raise KnowledgeValidationError("source_dir cannot be empty")
    if "\x00" in cleaned:
        raise KnowledgeValidationError("source_dir contains invalid characters")

    root_resolved = repo_root.resolve()
    raw_path = Path(cleaned)
    candidate = (
        raw_path.resolve() if raw_path.is_absolute() else (root_resolved / raw_path).resolve()
    )

    if not candidate.is_relative_to(root_resolved):
        raise KnowledgeValidationError("source_dir must resolve inside the repository root")
    if not candidate.exists():
        raise KnowledgeSourceNotFoundError(f"Knowledge source directory not found: {cleaned}")
    if not candidate.is_dir():
        raise KnowledgeValidationError(f"Knowledge source path is not a directory: {cleaned}")

    relative_posix = candidate.relative_to(root_resolved).as_posix()
    return candidate, relative_posix


@lru_cache(maxsize=1)
def _build_default_retriever() -> HybridRetriever:
    settings = get_settings()
    embedder = SentenceTransformerEmbedder(
        model_name=settings.EMBEDDING_MODEL_NAME,
        device="cpu",
    )
    vector_store = QdrantVectorStore(
        url=settings.QDRANT_URL,
        collection_name=settings.QDRANT_COLLECTION,
    )
    return HybridRetriever(
        embedder=embedder,
        vector_store=vector_store,
        bm25_path=DEFAULT_BM25_INDEX_PATH,
    )


def get_knowledge_retriever() -> HybridRetriever:
    """FastAPI dependency returning the process-wide HybridRetriever instance."""
    return _build_default_retriever()


def _resolve_embedding_model_name(retriever: HybridRetriever) -> str:
    return str(getattr(retriever.embedder, "model_name", type(retriever.embedder).__name__))


def index_knowledge_base(
    source_dir: str = DEFAULT_KNOWLEDGE_DIR,
    *,
    max_chars: int = 1200,
    overlap_chars: int = 150,
    retriever: HybridRetriever | None = None,
    repo_root: Path = REPO_ROOT,
) -> KnowledgeIndexResult:
    """Load, chunk, embed, and idempotently index Markdown documents into Qdrant + BM25."""
    resolved_dir, relative_dir = resolve_knowledge_source_dir(source_dir, repo_root=repo_root)

    try:
        chunker = MarkdownChunker(max_chars=max_chars, overlap_chars=overlap_chars)
        documents = load_knowledge_base(resolved_dir)
        chunks = chunker.chunk_documents(documents)
    except FileNotFoundError as exc:
        raise KnowledgeSourceNotFoundError(str(exc)) from exc
    except ValueError as exc:
        raise KnowledgeValidationError(str(exc)) from exc

    active_retriever = retriever if retriever is not None else get_knowledge_retriever()

    try:
        chunks_indexed = active_retriever.index_chunks(chunks)
    except _QDRANT_TRANSPORT_ERRORS as exc:
        logger.error(
            "Qdrant unavailable during knowledge indexing (collection=%s, error=%s)",
            active_retriever.vector_store.collection_name,
            type(exc).__name__,
        )
        raise QdrantUnavailableError(
            "Vector store unavailable while indexing knowledge base"
        ) from exc

    return KnowledgeIndexResult(
        source_dir=relative_dir,
        documents_indexed=len(documents),
        chunks_indexed=chunks_indexed,
        collection=active_retriever.vector_store.collection_name,
        embedding_model=_resolve_embedding_model_name(active_retriever),
    )


def search_knowledge(
    query: str,
    *,
    top_k: int = 5,
    mode: RetrievalMode = "hybrid",
    service: str | None = None,
    doc_type: DocType | None = None,
    retriever: HybridRetriever | None = None,
) -> list[RetrievedChunk]:
    """Run hybrid, dense, or BM25 retrieval against the indexed operations knowledge base."""
    active_retriever = retriever if retriever is not None else get_knowledge_retriever()

    try:
        return active_retriever.search(
            query,
            top_k=top_k,
            mode=mode,
            service=service,
            doc_type=doc_type,
        )
    except ValueError as exc:
        raise KnowledgeValidationError(str(exc)) from exc
    except _QDRANT_TRANSPORT_ERRORS as exc:
        logger.error(
            "Qdrant unavailable during knowledge search (collection=%s, mode=%s, error=%s)",
            active_retriever.vector_store.collection_name,
            mode,
            type(exc).__name__,
        )
        raise QdrantUnavailableError(
            "Vector store unavailable while searching knowledge base"
        ) from exc
