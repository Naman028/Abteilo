"""Text embeddings for hybrid search, from a local Ollama model.

An embedder turns text into vectors. It is not a generative model: it never
produces text, and it sees only document chunks and search queries.
"""

from typing import Protocol

import httpx

from abteilo.config import get_settings

MAX_INPUT_CHARS = 2000
BATCH_SIZE = 16

# Some models are trained with task prefixes; using them measurably improves recall.
_PREFIXES: dict[str, tuple[str, str]] = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
}


class EmbeddingError(Exception):
    """Carries a stable, safe code only."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Embedder(Protocol):
    model: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class OllamaEmbedder:
    def __init__(self, model: str):
        self.model = model
        self._query_prefix, self._doc_prefix = _PREFIXES.get(model.split(":")[0], ("", ""))

    def _embed(self, texts: list[str], timeout: float) -> list[list[float]]:
        # Never cut input silently: a truncated chunk would be searchable by only part of its text.
        if any(len(t) > MAX_INPUT_CHARS for t in texts):
            raise EmbeddingError("EMBEDDING_INPUT_TOO_LONG")
        settings = get_settings()
        try:
            # truncate=False: Ollama must refuse over-long input instead of embedding a prefix.
            r = httpx.post(f"{settings.ollama_url.rstrip('/')}/api/embed",
                           json={"model": self.model, "input": texts, "truncate": False}, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise EmbeddingError("EMBEDDING_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError("EMBEDDING_UNAVAILABLE") from exc
        if r.status_code == 404:
            raise EmbeddingError("EMBEDDING_MODEL_NOT_INSTALLED")
        if r.status_code == 400:
            raise EmbeddingError("EMBEDDING_INPUT_REJECTED")
        if r.status_code != 200:
            raise EmbeddingError("EMBEDDING_UNAVAILABLE")
        vectors = r.json().get("embeddings") or []
        if len(vectors) != len(texts) or not all(vectors) or len({len(v) for v in vectors}) != 1:
            raise EmbeddingError("EMBEDDING_INVALID_RESPONSE")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        timeout = get_settings().embedding_index_timeout_seconds
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            out += self._embed([self._doc_prefix + t for t in texts[i:i + BATCH_SIZE]], timeout)
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_prefix + text], get_settings().embedding_query_timeout_seconds)[0]


_override: list["Embedder | None"] = []  # empty = use configuration


def set_embedder(embedder: "Embedder | None") -> None:
    """Tests and the evaluation harness install a specific embedder (None = embeddings off)."""
    _override[:] = [embedder]


def reset_embedder() -> None:
    _override.clear()


def get_embedder() -> Embedder | None:
    if _override:
        return _override[0]
    model = get_settings().embedding_model.strip()
    return OllamaEmbedder(model) if model else None


def check_dimensions(vectors: list[list[float]], expected: int | None) -> None:
    """Vectors of one model must all have the size already stored for that model."""
    sizes = {len(v) for v in vectors}
    if len(sizes) > 1 or (expected is not None and sizes and sizes != {expected}):
        raise EmbeddingError("EMBEDDING_DIMENSION_MISMATCH")


def to_pgvector(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"
