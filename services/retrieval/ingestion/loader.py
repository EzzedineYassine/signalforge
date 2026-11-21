from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DocType = Literal["runbook", "incident", "architecture", "deployment"]
VALID_DOC_TYPES: tuple[str, ...] = ("runbook", "incident", "architecture", "deployment")
_REQUIRED_KEYS: tuple[str, ...] = ("doc_id", "doc_type", "title")


class KnowledgeDocument(BaseModel):
    """Parsed Markdown knowledge base document with validated frontmatter metadata."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    doc_type: DocType
    title: str
    service: str
    tags: list[str] = Field(default_factory=list)
    is_synthetic: bool = False
    source_path: str
    content: str


def _parse_scalar(raw_value: str) -> Any:
    val = raw_value.strip()
    if not val:
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    lower = val.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_unquote(part.strip()) for part in inner.split(",") if part.strip()]
    return val


def _unquote(text: str) -> str:
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        return text[1:-1]
    return text


def parse_frontmatter(
    raw_text: str, *, source_label: str = "<memory>"
) -> tuple[dict[str, Any], str]:
    """Parse YAML-style frontmatter delimited by leading/closing '---' lines using stdlib only."""
    text = raw_text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{source_label}: missing opening YAML frontmatter delimiter '---'")

    closing_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            closing_idx = idx
            break

    if closing_idx is None:
        raise ValueError(f"{source_label}: missing closing YAML frontmatter delimiter '---'")

    metadata: dict[str, Any] = {}
    current_list_key: str | None = None

    for offset, raw_line in enumerate(lines[1:closing_idx], start=2):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("- "):
            if current_list_key is None:
                raise ValueError(
                    f"{source_label}: malformed frontmatter list item at line {offset}"
                )
            metadata[current_list_key].append(_unquote(stripped[2:].strip()))
            continue

        if ":" not in raw_line:
            raise ValueError(
                f"{source_label}: malformed frontmatter at line {offset}: expected 'key: value'"
            )

        key_raw, val_raw = raw_line.split(":", 1)
        key = key_raw.strip()
        if not key:
            raise ValueError(f"{source_label}: empty frontmatter key at line {offset}")

        if not val_raw.strip():
            metadata[key] = []
            current_list_key = key
        else:
            metadata[key] = _parse_scalar(val_raw)
            current_list_key = None

    body = "\n".join(lines[closing_idx + 1 :]).strip()
    return metadata, body


def load_markdown_document(
    path: Path,
    *,
    base_dir: Path | None = None,
) -> KnowledgeDocument:
    """Load and validate a single Markdown knowledge document from disk."""
    source_label = (
        path.relative_to(base_dir).as_posix() if base_dir is not None else path.as_posix()
    )
    raw_text = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(raw_text, source_label=source_label)

    missing = [
        key
        for key in _REQUIRED_KEYS
        if key not in metadata or not isinstance(metadata[key], str) or not metadata[key].strip()
    ]
    if missing:
        raise ValueError(
            f"{source_label}: missing required frontmatter key(s): {', '.join(missing)}"
        )

    doc_type = metadata["doc_type"].strip().lower()
    if doc_type not in VALID_DOC_TYPES:
        raise ValueError(
            f"{source_label}: invalid doc_type {doc_type!r}; expected one of {VALID_DOC_TYPES}"
        )

    raw_service = metadata.get("service", "platform")
    if not isinstance(raw_service, str) or not raw_service.strip():
        raise ValueError(f"{source_label}: frontmatter 'service' must be a non-empty string")

    raw_tags = metadata.get("tags", [])
    if isinstance(raw_tags, str):
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        raise ValueError(f"{source_label}: frontmatter 'tags' must be a list of strings")

    raw_synthetic = metadata.get("is_synthetic", False)
    if not isinstance(raw_synthetic, bool):
        raise ValueError(f"{source_label}: frontmatter 'is_synthetic' must be a boolean")

    if not body:
        raise ValueError(f"{source_label}: document body cannot be empty")

    return KnowledgeDocument(
        doc_id=metadata["doc_id"].strip(),
        doc_type=doc_type,  # type: ignore[arg-type]
        title=metadata["title"].strip(),
        service=raw_service.strip(),
        tags=tags,
        is_synthetic=raw_synthetic,
        source_path=source_label,
        content=body,
    )


def load_knowledge_base(root_dir: Path) -> list[KnowledgeDocument]:
    """Load all Markdown documents recursively from root_dir with duplicate doc_id protection."""
    if not root_dir.exists() or not root_dir.is_dir():
        raise FileNotFoundError(f"Knowledge base directory does not exist: {root_dir}")

    md_files = sorted(root_dir.rglob("*.md"), key=lambda p: p.relative_to(root_dir).as_posix())
    if not md_files:
        raise FileNotFoundError(f"No Markdown documents found under {root_dir}")

    docs: list[KnowledgeDocument] = []
    seen_ids: set[str] = set()
    for md_path in md_files:
        doc = load_markdown_document(md_path, base_dir=root_dir)
        if doc.doc_id in seen_ids:
            raise ValueError(
                f"{doc.source_path}: duplicate doc_id {doc.doc_id!r} detected in knowledge base"
            )
        seen_ids.add(doc.doc_id)
        docs.append(doc)
    return docs
