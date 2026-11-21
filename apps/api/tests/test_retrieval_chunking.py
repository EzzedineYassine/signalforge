import ast
from pathlib import Path
from uuid import UUID

import pytest

from services.retrieval import (
    VALID_DOC_TYPES,
    KnowledgeDocument,
    MarkdownChunker,
    load_knowledge_base,
    load_markdown_document,
    parse_frontmatter,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
KB_ROOT = REPO_ROOT / "data" / "synthetic" / "knowledge_base"


class TestFrontmatterAndLoader:
    def test_parse_valid_frontmatter_inline_and_multiline_lists(self) -> None:
        raw = (
            "---\n"
            "doc_id: rb-test\n"
            "doc_type: runbook\n"
            'title: "Test Runbook: CPU"\n'
            "service: checkout-service\n"
            "tags:\n"
            "  - cpu\n"
            "  - latency\n"
            "is_synthetic: true\n"
            "---\n\n"
            "# Heading\n\nBody text.\n"
        )
        meta, body = parse_frontmatter(raw, source_label="test.md")
        assert meta["doc_id"] == "rb-test"
        assert meta["doc_type"] == "runbook"
        assert meta["title"] == "Test Runbook: CPU"
        assert meta["tags"] == ["cpu", "latency"]
        assert meta["is_synthetic"] is True
        assert body == "# Heading\n\nBody text."

    @pytest.mark.parametrize(
        ("content", "match"),
        [
            ("# No frontmatter here\n", "missing opening"),
            ("---\ndoc_id: a\n# never closed", "missing closing"),
            ("---\nnot_a_valid_line\n---\nBody", "malformed frontmatter"),
            ("---\ndoc_id: a\ndoc_type: runbook\n---\nBody", "missing required frontmatter"),
            (
                "---\ndoc_id: a\ndoc_type: invalid_type\ntitle: T\n---\nBody",
                "invalid doc_type",
            ),
            (
                "---\ndoc_id: a\ndoc_type: runbook\ntitle: T\nis_synthetic: maybe\n---\nBody",
                "boolean",
            ),
            ("---\ndoc_id: a\ndoc_type: runbook\ntitle: T\n---\n   \n", "body cannot be empty"),
        ],
    )
    def test_loader_rejects_malformed_or_missing_frontmatter(
        self, tmp_path: Path, content: str, match: str
    ) -> None:
        bad_file = tmp_path / "broken.md"
        bad_file.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match=match) as exc_info:
            load_markdown_document(bad_file, base_dir=tmp_path)
        assert "broken.md" in str(exc_info.value)

    def test_duplicate_doc_id_rejected(self, tmp_path: Path) -> None:
        doc = "---\ndoc_id: dup-1\ndoc_type: runbook\ntitle: Dup\n---\nBody\n"
        (tmp_path / "a.md").write_text(doc, encoding="utf-8")
        (tmp_path / "b.md").write_text(doc, encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate doc_id"):
            load_knowledge_base(tmp_path)


class TestMarkdownChunker:
    def test_invalid_chunker_params_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_chars"):
            MarkdownChunker(max_chars=0, overlap_chars=0)
        with pytest.raises(ValueError, match="overlap_chars"):
            MarkdownChunker(max_chars=100, overlap_chars=-1)
        with pytest.raises(ValueError, match="strictly less"):
            MarkdownChunker(max_chars=100, overlap_chars=100)

    def test_section_heading_preservation_code_fences_and_deterministic_ids(self) -> None:
        doc = KnowledgeDocument(
            doc_id="rb-demo",
            doc_type="runbook",
            title="Demo Runbook",
            service="checkout-service",
            tags=["demo"],
            is_synthetic=True,
            source_path="runbooks/demo.md",
            content=(
                "# Demo Runbook\n\n"
                "## Symptoms\n\nHigh CPU usage.\n\n"
                "### Commands\n\n"
                "```bash\n"
                "# this bash comment is NOT a markdown heading\n"
                "ps aux | head\n"
                "```\n\n"
                "## Remediation\n\nScale replicas.\n"
            ),
        )
        chunker = MarkdownChunker(max_chars=1200, overlap_chars=150)
        chunks_a = chunker.chunk_document(doc)
        chunks_b = chunker.chunk_document(doc)

        assert chunks_a == chunks_b
        assert [c.chunk_index for c in chunks_a] == [0, 1, 2]
        assert [c.section_heading for c in chunks_a] == [
            "Demo Runbook > Symptoms",
            "Demo Runbook > Symptoms > Commands",
            "Demo Runbook > Remediation",
        ]
        assert "# this bash comment is NOT a markdown heading" in chunks_a[1].text
        for chunk in chunks_a:
            parsed_uuid = UUID(chunk.chunk_id)
            assert parsed_uuid.version == 5

    def test_subchunking_long_section_respects_max_chars_and_overlap(self) -> None:
        sentences = [f"Sentence number {i:02d} describes diagnostic telemetry." for i in range(40)]
        long_body = "## Deep Analysis\n\n" + " ".join(sentences)
        doc = KnowledgeDocument(
            doc_id="arch-long",
            doc_type="architecture",
            title="Long Architecture Doc",
            service="order-service",
            tags=["arch"],
            is_synthetic=True,
            source_path="architecture/long.md",
            content=long_body,
        )

        chunker = MarkdownChunker(max_chars=300, overlap_chars=60)
        chunks = chunker.chunk_document(doc)

        assert len(chunks) >= 5
        for chunk in chunks:
            assert len(chunk.text) <= 300
            assert chunk.section_heading == "Deep Analysis"
            assert chunk.doc_id == "arch-long"

        # Consecutive sub-chunks must share overlapping tail/head words
        for prev_chunk, next_chunk in zip(chunks, chunks[1:], strict=False):
            tail_words = set(prev_chunk.text[-80:].split())
            head_words = set(next_chunk.text[:80].split())
            assert len(tail_words & head_words) >= 2


class TestSyntheticKnowledgeBaseCorpus:
    def test_load_and_chunk_all_37_synthetic_documents(self) -> None:
        docs = load_knowledge_base(KB_ROOT)
        assert len(docs) == 37

        counts_by_type = {t: sum(1 for d in docs if d.doc_type == t) for t in VALID_DOC_TYPES}
        assert counts_by_type == {
            "runbook": 15,
            "incident": 10,
            "architecture": 6,
            "deployment": 6,
        }

        for doc in docs:
            assert doc.is_synthetic is True
            assert "[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]" in doc.content

        chunker = MarkdownChunker(max_chars=1200, overlap_chars=150)
        chunks = chunker.chunk_documents(docs)

        assert len(chunks) > len(docs)
        chunk_ids = [c.chunk_id for c in chunks]
        assert len(chunk_ids) == len(set(chunk_ids))

        for chunk in chunks:
            assert chunk.is_synthetic is True
            assert chunk.doc_type in VALID_DOC_TYPES
            assert chunk.section_heading.strip() != ""
            assert chunk.text.strip() != ""
            assert len(chunk.text) <= 1200
            assert UUID(chunk.chunk_id).version == 5


def test_retrieval_package_has_no_framework_or_db_imports() -> None:
    forbidden = {"fastapi", "sqlalchemy", "psycopg", "alembic", "starlette", "pickle", "joblib"}
    pkg_dir = REPO_ROOT / "services" / "retrieval"
    py_files = sorted(pkg_dir.rglob("*.py"))
    assert len(py_files) >= 5

    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        assert imported_roots.isdisjoint(forbidden), (
            f"{path.relative_to(REPO_ROOT)} imported forbidden: {imported_roots & forbidden}"
        )
