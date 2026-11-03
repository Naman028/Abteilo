"""Embedding index maintenance (RAG-04).

New document versions start as `pending`. The worker embeds their chunks and
marks them `indexed`, or `failed` after MAX_ATTEMPTS with a safe error code.
Embeddings are computed outside any database transaction, so no lock is held
while the model runs. The write-back is conditional on the version still being
pending, so retries and concurrent workers cannot double-write.

Search never depends on indexing for safety: visibility is decided at query time
from live grants and document status, so a pending or failed index can only
reduce recall, never widen access (RAG-03, OPS-01).

    python -m abteilo.indexer      # index everything pending once
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from abteilo.ai.embeddings import Embedder, EmbeddingError, check_dimensions, get_embedder, to_pgvector
from abteilo.db import transaction

MAX_ATTEMPTS = 3
VERSIONS_PER_RUN = 20
log = logging.getLogger("abteilo.indexer")


@dataclass(frozen=True)
class IndexRun:
    indexed: int = 0
    failed: int = 0
    retried: int = 0


def index_company(company_id: UUID, embedder: Embedder, limit: int = VERSIONS_PER_RUN) -> IndexRun:
    with transaction(company_id) as conn:
        versions = conn.execute(text(
            "SELECT id FROM core.document_versions WHERE index_status = 'pending' ORDER BY created_at LIMIT :n"
        ), {"n": limit}).scalars().all()
    indexed = failed = retried = 0
    for version_id in versions:
        with transaction(company_id) as conn:
            chunks = conn.execute(text(
                "SELECT c.id, c.content, d.title FROM core.document_chunks c JOIN core.documents d ON d.id = c.document_id"
                " WHERE c.version_id = :v ORDER BY c.chunk_index"
            ), {"v": version_id}).all()
            expected_dims = conn.execute(text(
                "SELECT embedding_dims FROM core.document_versions WHERE embedding_model = :m AND index_status = 'indexed'"
                " AND embedding_dims IS NOT NULL ORDER BY indexed_at DESC LIMIT 1"
            ), {"m": embedder.model}).scalar()
        try:
            # The title gives every chunk its document context (a chunk alone often lacks the topic word).
            vectors = embedder.embed_documents([f"{c.title}\n\n{c.content}" for c in chunks]) if chunks else []
            check_dimensions(vectors, expected_dims)  # never mix vector sizes for one model (RAG-05)
        except EmbeddingError as exc:
            status = _record_failure(company_id, version_id, exc.code)
            failed += status == "failed"
            retried += status == "pending"
            continue
        with transaction(company_id) as conn:
            claimed = conn.execute(text(
                "UPDATE core.document_versions SET index_status = 'indexed', indexed_at = now(), index_error_code = NULL,"
                " embedding_model = :m, embedding_dims = :dims WHERE id = :v AND index_status = 'pending' RETURNING id"
            ), {"m": embedder.model, "dims": len(vectors[0]) if vectors else None, "v": version_id}).first()
            if claimed is None:
                continue  # another worker finished it first; its result stands
            for chunk, vector in zip(chunks, vectors, strict=True):
                conn.execute(text(
                    "UPDATE core.document_chunks SET embedding = CAST(:e AS vector), embedding_model = :m, embedded_at = now()"
                    " WHERE id = :id"
                ), {"e": to_pgvector(vector), "m": embedder.model, "id": chunk.id})
        indexed += 1
    return IndexRun(indexed, failed, retried)


def _record_failure(company_id: UUID, version_id: UUID, code: str) -> str | None:
    with transaction(company_id) as conn:
        return conn.execute(text(
            "UPDATE core.document_versions SET index_attempts = index_attempts + 1, index_error_code = :e,"
            " index_status = CASE WHEN index_attempts + 1 >= :max THEN 'failed' ELSE 'pending' END"
            " WHERE id = :v AND index_status = 'pending' RETURNING index_status"
        ), {"e": code, "max": MAX_ATTEMPTS, "v": version_id}).scalar()


def index_all(embedder: Embedder | None = None) -> IndexRun:
    embedder = embedder or get_embedder()
    if embedder is None:
        return IndexRun()
    with transaction() as conn:
        companies = conn.execute(text("SELECT DISTINCT company_id FROM core.memberships")).scalars().all()
    total = IndexRun()
    for company_id in companies:
        run = index_company(company_id, embedder)
        total = IndexRun(total.indexed + run.indexed, total.failed + run.failed, total.retried + run.retried)
    return total


def retry_failed(conn) -> int:
    """Owner action: put failed versions back in the queue (current company context)."""
    return conn.execute(text(
        "UPDATE core.document_versions SET index_status = 'pending', index_attempts = 0 WHERE index_status = 'failed'"
    )).rowcount


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run = index_all()
    log.info('{"event":"index_run","indexed":%d,"failed":%d,"retried":%d}', run.indexed, run.failed, run.retried)


if __name__ == "__main__":
    main()
