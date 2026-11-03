"""Graph-read audit log (INT-09).

Purpose: reconstruct who looked at which graph resource during an incident.
It is not a monitoring tool: the console shows no per-person statistics, and
only holders of `audit.read` can export it, by time range and resource.

Metadata only: actor, operation, root kind and reference, depth, outcome and
node count. No labels, no response content, no question text.

Retention is enforced by the database. The runtime role may insert and read
(within its tenant). It may delete only rows older than 90 days, and only inside
the retention job's context (`app.retention_job`). There is no update grant.

Revision ID: 0008
Revises: 0007
"""

import os

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

RUNTIME_ROLE = os.environ.get("ABTEILO_RUNTIME_ROLE", "abteilo_app")
RETENTION_DAYS = 90


def upgrade() -> None:
    op.execute("""
        CREATE TABLE core.graph_read_log (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES core.companies(id),
            occurred_at timestamptz NOT NULL DEFAULT now(),
            actor_user_id uuid NOT NULL REFERENCES core.users(id),
            request_id text CHECK (request_id ~ '^[0-9a-f]{32}$'),
            operation text NOT NULL CHECK (operation IN ('neighbourhood', 'path', 'assistant')),
            root_kind text CHECK (root_kind IN ('department', 'project', 'task', 'person', 'purchase_request',
                                                'leave_request', 'access_request', 'document')),
            root_ref_id uuid,
            target_ref_id uuid,
            depth integer NOT NULL CHECK (depth BETWEEN 1 AND 4),
            outcome text NOT NULL CHECK (outcome IN ('ok', 'not_found', 'timeout')),
            node_count integer NOT NULL DEFAULT 0 CHECK (node_count BETWEEN 0 AND 1000)
        )""")
    op.execute("CREATE INDEX graph_read_log_company_time_idx ON core.graph_read_log (company_id, occurred_at)")
    op.execute("ALTER TABLE core.graph_read_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE core.graph_read_log FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_read ON core.graph_read_log FOR SELECT USING (company_id = core.current_company_id())")
    op.execute("CREATE POLICY tenant_insert ON core.graph_read_log FOR INSERT WITH CHECK (company_id = core.current_company_id())")
    op.execute(f"""CREATE POLICY retention_purge ON core.graph_read_log FOR DELETE
                   USING (company_id = core.current_company_id() AND core.retention_job()
                          AND occurred_at < now() - interval '{RETENTION_DAYS} days')""")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON core.graph_read_log TO {RUNTIME_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE core.graph_read_log_id_seq TO {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE core.graph_read_log")
