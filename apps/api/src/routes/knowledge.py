from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.src.schemas.knowledge import (
    KnowledgeIndexRequest,
    KnowledgeIndexResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    RetrievedChunkResponse,
)
from apps.api.src.services.knowledge_service import (
    KnowledgeSourceNotFoundError,
    KnowledgeValidationError,
    QdrantUnavailableError,
    get_knowledge_retriever,
    index_knowledge_base,
    search_knowledge,
)
from services.retrieval.search.hybrid import HybridRetriever

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


@router.post("/index", response_model=KnowledgeIndexResponse, status_code=status.HTTP_200_OK)
def index_knowledge(
    payload: KnowledgeIndexRequest,
    retriever: Annotated[HybridRetriever, Depends(get_knowledge_retriever)],
) -> KnowledgeIndexResponse:
    try:
        result = index_knowledge_base(
            source_dir=payload.source_dir,
            max_chars=payload.max_chars,
            overlap_chars=payload.overlap_chars,
            retriever=retriever,
        )
    except KnowledgeSourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except KnowledgeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except QdrantUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return KnowledgeIndexResponse(
        source_dir=result.source_dir,
        documents_indexed=result.documents_indexed,
        chunks_indexed=result.chunks_indexed,
        collection=result.collection,
        embedding_model=result.embedding_model,
    )


@router.post("/search", response_model=KnowledgeSearchResponse, status_code=status.HTTP_200_OK)
def search_knowledge_endpoint(
    payload: KnowledgeSearchRequest,
    retriever: Annotated[HybridRetriever, Depends(get_knowledge_retriever)],
) -> KnowledgeSearchResponse:
    try:
        hits = search_knowledge(
            query=payload.query,
            top_k=payload.top_k,
            mode=payload.mode,
            service=payload.service,
            doc_type=payload.doc_type,
            retriever=retriever,
        )
    except KnowledgeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except QdrantUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    results = [RetrievedChunkResponse.model_validate(hit) for hit in hits]
    return KnowledgeSearchResponse(
        query=payload.query,
        mode=payload.mode,
        total=len(results),
        results=results,
    )
