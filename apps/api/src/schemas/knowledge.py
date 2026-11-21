from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.api.src.services.knowledge_service import DEFAULT_KNOWLEDGE_DIR
from services.ingestion.normalizer import normalize_service_name
from services.retrieval.ingestion.loader import DocType
from services.retrieval.search.models import RetrievalMode


class KnowledgeIndexRequest(BaseModel):
    source_dir: str = DEFAULT_KNOWLEDGE_DIR
    max_chars: int = Field(default=1200, ge=100, le=8000)
    overlap_chars: int = Field(default=150, ge=0, le=2000)

    @field_validator("source_dir")
    @classmethod
    def _validate_source_dir(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("source_dir cannot be empty")
        return cleaned

    @model_validator(mode="after")
    def _validate_chunk_params(self) -> "KnowledgeIndexRequest":
        if self.overlap_chars >= self.max_chars:
            raise ValueError("overlap_chars must be strictly less than max_chars")
        return self


class KnowledgeIndexResponse(BaseModel):
    source_dir: str
    documents_indexed: int
    chunks_indexed: int
    collection: str
    embedding_model: str


class KnowledgeSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=50)
    mode: RetrievalMode = "hybrid"
    service: str | None = None
    doc_type: DocType | None = None

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("query cannot be empty")
        return cleaned

    @field_validator("service", mode="before")
    @classmethod
    def _validate_service(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_service_name(value)


class RetrievedChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_id: str
    doc_id: str
    doc_type: DocType
    title: str
    service: str
    section_heading: str
    chunk_index: int
    source_path: str
    is_synthetic: bool
    text: str
    score: float
    retrieval_source: RetrievalMode


class KnowledgeSearchResponse(BaseModel):
    query: str
    mode: RetrievalMode
    total: int
    results: list[RetrievedChunkResponse]
