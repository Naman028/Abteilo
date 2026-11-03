"""Hybrid retrieval: chunk embeddings (pgvector) and per-version index status.

- RAG-02: embeddings live on the immutable chunk rows of one document version.
  The runtime role may write only the embedding columns and the index-status
  columns (column-level grants); content stays immutable.
- RAG-04: each document version records whether it is pending, indexed or
  failed, with attempts and a safe error code.
- Embeddings are stored with the model that produced them, so a model change
  never mixes vector spaces: search only compares vectors of the active model.

The `vector` extension must be installable by the migration role. In Docker the
owner role is the database superuser; on a self-managed server, a superuser runs
`CREATE EXTENSION vector` once beforehand.

Revision ID: 0003
Revises: 0002
"""

import os

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

RUNTIME_ROLE = os.environ.get("ABTEILO_RUNTIME_ROLE", "abteilo_app")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")  # the vector type and operators live there

    op.execute("""
        ALTER TABLE core.document_chunks
          ADD COLUMN embedding vector,
          ADD COLUMN embedding_model text,
          ADD COLUMN embedded_at timestamptz,
          ADD CONSTRAINT document_chunks_embedding_model_ck
            CHECK ((embedding IS NULL) = (embedding_model IS NULL) AND (embedding IS NULL) = (embedded_at IS NULL))
    """)
    op.execute("""
        ALTER TABLE core.document_versions
          ADD COLUMN index_status text NOT NULL DEFAULT 'pending'
            CHECK (index_status IN ('pending', 'indexed', 'failed')),
          ADD COLUMN index_attempts integer NOT NULL DEFAULT 0 CHECK (index_attempts BETWEEN 0 AND 100),
          ADD COLUMN index_error_code text CHECK (index_error_code ~ '^[A-Z_]{1,40}$'),
          ADD COLUMN indexed_at timestamptz,
          ADD COLUMN embedding_model text
    """)
    op.execute("CREATE INDEX document_versions_pending_idx ON core.document_versions (company_id, created_at)"
               " WHERE index_status = 'pending'")

    op.execute(f"GRANT UPDATE (embedding, embedding_model, embedded_at) ON core.document_chunks TO {RUNTIME_ROLE}")
    op.execute(f"GRANT UPDATE (index_status, index_attempts, index_error_code, indexed_at, embedding_model)"
               f" ON core.document_versions TO {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS core.document_versions_pending_idx")
    op.execute("""
        ALTER TABLE core.document_versions
          DROP COLUMN embedding_model, DROP COLUMN indexed_at, DROP COLUMN index_error_code,
          DROP COLUMN index_attempts, DROP COLUMN index_status
    """)
    op.execute("""
        ALTER TABLE core.document_chunks
          DROP CONSTRAINT document_chunks_embedding_model_ck,
          DROP COLUMN embedded_at, DROP COLUMN embedding_model, DROP COLUMN embedding
    """)
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {RUNTIME_ROLE}")
    # The extension itself is left installed: other schemas may use it, and dropping it needs a superuser.
