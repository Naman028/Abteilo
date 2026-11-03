"""Generated reports (AI-authored PDF / Word deliverables).

A report is stored as its typed, validated content (JSON), never as a file: PDF and Word are rendered in
memory on each download, so there is no file path, no file store and nothing to clean up on disk.

- Private to the person who generated it, like chats (owner_scope RLS, FORCE). Not even the company owner
  can read another person's report.
- Immutable: the runtime role may insert, read and delete, never update.
- Expiring: expires_at is set at creation (at most 90 days, enforced here); the retention job deletes expired
  rows. Deleting the chat that produced a report deletes the report (ON DELETE CASCADE).
- Bounded: content is limited to 1 MB.
- Downloads re-check the requester's current permissions (application layer); an old report never outlives
  a revoked permission.

Also allows the 'report' stage in core.ai_runs, so report generation is metered like other model calls.

Revision ID: 0009
Revises: 0008
"""

import os

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

RUNTIME_ROLE = os.environ.get("ABTEILO_RUNTIME_ROLE", "abteilo_app")


def upgrade() -> None:
    op.execute("""
        CREATE TABLE conv.reports (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES core.companies(id),
            user_id uuid NOT NULL REFERENCES core.users(id),
            department_id uuid NOT NULL,
            conversation_id uuid REFERENCES conv.conversations(id) ON DELETE CASCADE,
            kind text NOT NULL CHECK (kind IN ('engineering_progress', 'finance_status', 'hr_leave', 'it_access',
                                               'company_status')),
            title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
            complete boolean NOT NULL,
            content jsonb NOT NULL CHECK (octet_length(content::text) <= 1000000),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            CHECK (expires_at > created_at AND expires_at <= created_at + interval '90 days')
        )""")
    op.execute("CREATE INDEX reports_owner_idx ON conv.reports (company_id, user_id, created_at DESC)")
    op.execute("CREATE INDEX reports_expiry_idx ON conv.reports (company_id, expires_at)")
    op.execute("ALTER TABLE conv.reports ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conv.reports FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY owner_scope ON conv.reports
                  USING (company_id = core.current_company_id() AND (user_id = core.current_user_id() OR core.retention_job()))
                  WITH CHECK (company_id = core.current_company_id() AND user_id = core.current_user_id())""")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON conv.reports TO {RUNTIME_ROLE}")
    op.execute("ALTER TABLE core.ai_runs DROP CONSTRAINT ai_runs_stage_check")
    op.execute("ALTER TABLE core.ai_runs ADD CONSTRAINT ai_runs_stage_check CHECK (stage IN ('plan', 'answer', 'extract', 'report'))")


def downgrade() -> None:
    # Usage history is never discarded to make a downgrade fit; an operator must decide what to do with it.
    # FORCE RLS binds the owner too; lift it for this one check (same transaction), then restore it.
    op.execute("ALTER TABLE core.ai_runs NO FORCE ROW LEVEL SECURITY")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM core.ai_runs WHERE stage = 'report') THEN
            RAISE EXCEPTION 'core.ai_runs holds report-stage runs; downgrading would invalidate them';
        END IF; END $$""")
    op.execute("ALTER TABLE core.ai_runs FORCE ROW LEVEL SECURITY")
    # Generated reports are derived, expiring copies of business data (regenerable); they are dropped.
    op.execute("ALTER TABLE core.ai_runs DROP CONSTRAINT ai_runs_stage_check")
    op.execute("ALTER TABLE core.ai_runs ADD CONSTRAINT ai_runs_stage_check CHECK (stage IN ('plan', 'answer', 'extract'))")
    op.execute("DROP TABLE conv.reports")
