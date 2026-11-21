from typing import Literal

from pydantic import BaseModel, ConfigDict

from services.retrieval.ingestion.loader import DocType

RetrievalMode = Literal["hybrid", "dense", "bm25"]


class RetrievedChunk(BaseModel):
    """Retrieved document chunk carrying full citation metadata and relevance score."""

    model_config = ConfigDict(frozen=True)

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
