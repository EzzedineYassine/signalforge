import re
import uuid
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from services.retrieval.ingestion.loader import DocType, KnowledgeDocument

CHUNK_ID_NAMESPACE = uuid.UUID("7f3c8e2a-4d1b-5c9e-8a6b-1d2e3f4a5b6c")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*$")


class DocumentChunk(BaseModel):
    """Section-aware chunk with deterministic UUID5 identifier and full citation metadata."""

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


class MarkdownChunker:
    """Section-aware Markdown chunker preserving heading hierarchy and deterministic chunk IDs."""

    def __init__(self, max_chars: int = 1200, overlap_chars: int = 150) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be greater than 0")
        if overlap_chars < 0:
            raise ValueError("overlap_chars must be non-negative")
        if overlap_chars >= max_chars:
            raise ValueError("overlap_chars must be strictly less than max_chars")
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars

    @staticmethod
    def make_chunk_id(doc_id: str, chunk_index: int) -> str:
        """Compute deterministic UUID5 chunk identifier from doc_id and chunk_index."""
        return str(uuid.uuid5(CHUNK_ID_NAMESPACE, f"{doc_id}|{chunk_index}"))

    def _split_sections(self, content: str, fallback_heading: str) -> list[tuple[str, str]]:
        """Split Markdown into (section_heading, section_text) pairs while ignoring code fences."""
        heading_stack: dict[int, str] = {}
        current_heading = fallback_heading
        current_lines: list[str] = []
        sections: list[tuple[str, str]] = []
        in_code_fence = False

        def _flush() -> None:
            text = "\n".join(current_lines).strip()
            if text:
                sections.append((current_heading, text))

        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_fence = not in_code_fence
                current_lines.append(line)
                continue

            if not in_code_fence:
                match = _HEADING_RE.match(stripped)
                if match is not None:
                    _flush()
                    current_lines = []
                    level = len(match.group(1))
                    heading_text = match.group(2).strip()
                    heading_stack[level] = heading_text
                    for deeper in list(heading_stack):
                        if deeper > level:
                            del heading_stack[deeper]
                    current_heading = " > ".join(
                        heading_stack[lvl] for lvl in sorted(heading_stack)
                    )
                    continue

            current_lines.append(line)

        _flush()
        return sections

    def _subchunk_text(self, text: str) -> list[str]:
        """Split oversized section text into <= max_chars windows with overlap_chars context."""
        cleaned = text.strip()
        if len(cleaned) <= self.max_chars:
            return [cleaned]

        chunks: list[str] = []
        n = len(cleaned)
        start = 0

        while start < n:
            max_end = min(start + self.max_chars, n)
            if max_end == n:
                segment = cleaned[start:n].strip()
                if segment:
                    chunks.append(segment)
                break

            window = cleaned[start:max_end]
            split_rel = self._find_split_offset(window)
            end = start + split_rel
            segment = cleaned[start:end].strip()
            if segment:
                chunks.append(segment)

            next_start = max(end - self.overlap_chars, start + 1)
            while next_start < n and cleaned[next_start].isspace():
                next_start += 1
            if next_start <= start:
                next_start = end
            start = next_start

        return chunks

    def _find_split_offset(self, window: str) -> int:
        """Prefer paragraph, newline, sentence, or whitespace breaks in the latter half."""
        min_pos = max(1, len(window) // 2)
        for sep in ("\n\n", "\n", ". ", " "):
            pos = window.rfind(sep, min_pos)
            if pos != -1:
                return pos + len(sep) if sep == ". " else pos
        return len(window)

    def chunk_document(self, doc: KnowledgeDocument) -> list[DocumentChunk]:
        """Split a KnowledgeDocument into ordered, deterministic DocumentChunk objects."""
        sections = self._split_sections(doc.content, fallback_heading=doc.title)
        chunks: list[DocumentChunk] = []
        chunk_index = 0

        for heading, section_text in sections:
            for sub_text in self._subchunk_text(section_text):
                chunks.append(
                    DocumentChunk(
                        chunk_id=self.make_chunk_id(doc.doc_id, chunk_index),
                        doc_id=doc.doc_id,
                        doc_type=doc.doc_type,
                        title=doc.title,
                        service=doc.service,
                        section_heading=heading,
                        chunk_index=chunk_index,
                        source_path=doc.source_path,
                        is_synthetic=doc.is_synthetic,
                        text=sub_text,
                    )
                )
                chunk_index += 1

        return chunks

    def chunk_documents(self, docs: Sequence[KnowledgeDocument]) -> list[DocumentChunk]:
        """Chunk an ordered sequence of KnowledgeDocument objects."""
        all_chunks: list[DocumentChunk] = []
        for doc in docs:
            all_chunks.extend(self.chunk_document(doc))
        return all_chunks
