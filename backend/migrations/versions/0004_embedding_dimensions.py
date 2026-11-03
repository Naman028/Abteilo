"""Embedding dimensions: every stored vector records its size, per chunk and per version.

The `embedding` column is untyped (`vector` without a size) so the owner can change
models. A model whose output size changes (for example a different tag under the
same name) must not be compared with older vectors: pgvector raises an error on
mixed sizes. Chunks get a generated `embedding_dims`, versions record the size the
indexer wrote, search compares only vectors of the query's size, and the indexer
refuses to mix sizes for one model (RAG-05).

Existing indexed versions are backfilled from their chunks. FORCE ROW LEVEL
SECURITY applies to the table owner too, so the backfill lifts FORCE inside this
migration's transaction and restores it before commit.

Revision ID: 0004
Revises: 0003
"""

import os

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

RUNTIME_ROLE = os.environ.get("ABTEILO_RUNTIME_ROLE", "abteilo_app")


def upgrade() -> None:
    op.execute("""
        ALTER TABLE core.document_chunks
          ADD COLUMN embedding_dims integer GENERATED ALWAYS AS (vector_dims(embedding)) STORED
    """)
    op.execute("""
        ALTER TABLE core.document_versions
          ADD COLUMN embedding_dims integer CHECK (embedding_dims BETWEEN 1 AND 16000)
    """)
    op.execute("ALTER TABLE core.document_versions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE core.document_chunks NO FORCE ROW LEVEL SECURITY")
    op.execute("""
        UPDATE core.document_versions v SET embedding_dims = (
            SELECT max(c.embedding_dims) FROM core.document_chunks c WHERE c.version_id = v.id)
        WHERE v.index_status = 'indexed'
    """)
    op.execute("ALTER TABLE core.document_chunks FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE core.document_versions FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT UPDATE (embedding_dims) ON core.document_versions TO {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("ALTER TABLE core.document_versions DROP COLUMN embedding_dims")
    op.execute("ALTER TABLE core.document_chunks DROP COLUMN embedding_dims")
