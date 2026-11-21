import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for dense text embedding providers."""

    @property
    def dimension(self) -> int: ...

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class SentenceTransformerEmbedder:
    """CPU-backed Sentence-Transformers embedder with lazy model initialization."""

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        device: str = "cpu",
        batch_size: int = 32,
    ) -> None:
        if not model_name.strip():
            raise ValueError("model_name cannot be empty")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self._model: Any = None
        self._dimension: int | None = None

    def _get_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device=self.device)
            if hasattr(self._model, "get_embedding_dimension"):
                dim = self._model.get_embedding_dimension()
            else:
                dim = self._model.get_sentence_embedding_dimension()
            if dim is None:
                raise RuntimeError(f"Could not determine embedding dimension for {self.model_name}")
            self._dimension = int(dim)
        return self._model

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self._get_model()
        assert self._dimension is not None
        return self._dimension

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_model()
        vectors = model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(v) for v in row] for row in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]


class DeterministicHashEmbedder:
    """Fast, offline deterministic token-hashing embedder for unit and API tests."""

    def __init__(self, dimension: int = 384) -> None:
        if dimension < 1:
            raise ValueError("dimension must be at least 1")
        self._dimension = dimension
        self.model_name = f"deterministic-hash-{dimension}"

    @property
    def dimension(self) -> int:
        return self._dimension

    def _embed_single(self, text: str) -> list[float]:
        vec = [0.0] * self._dimension
        raw_tokens = _TOKEN_RE.findall(text.lower())
        tokens: list[str] = []
        for token in raw_tokens:
            tokens.append(token)
            if "-" in token or "_" in token:
                tokens.extend(part for part in re.split(r"[-_]", token) if part)

        if not tokens:
            vec[0] = 1.0
            return vec

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dimension
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            vec[bucket] += sign

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_single(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_single(text)
