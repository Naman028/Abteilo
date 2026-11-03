"""Knowledge graph (Block C): a provenance-backed projection of company records.

- graph_entities: one row per business record the graph knows (department, project,
  task, person, purchase/leave/access request, document), with the visibility scope
  of that record (GRAPH-01). Entities are deactivated, never deleted.
- graph_relationships: typed edges. `authority` says where an edge comes from:
  `record` (a foreign key in a business table), `document_mention` (a project code
  that literally occurs in the current document version) or `reviewed` (a
  document-derived relationship a person accepted). Edges are closed with
  `valid_to` + `ended_reason`, never deleted or reopened (GRAPH-05).
- graph_evidence: the source of every edge (GRAPH-03). A deferred constraint trigger
  refuses to commit an edge without evidence.
- graph_candidates: relationships a model suggested from a document. They are not
  edges: traversal never reads them, and only a reviewer's decision creates an
  edge from one (GRAPH-06).
- graph_extraction_runs / graph_projection_state: progress and health.

The runtime role may insert, and update only the columns that record state
changes; it may delete nothing here.

Revision ID: 0005
Revises: 0004
"""

import os

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

RUNTIME_ROLE = os.environ.get("ABTEILO_RUNTIME_ROLE", "abteilo_app")

TABLES = ["graph_entities", "graph_relationships", "graph_evidence", "graph_candidates",
          "graph_extraction_runs", "graph_projection_state"]

SCHEMA = r"""
CREATE TABLE core.graph_entities (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    kind text NOT NULL CHECK (kind IN ('department', 'project', 'task', 'person', 'purchase_request',
                                       'leave_request', 'access_request', 'document')),
    ref_id uuid NOT NULL,
    label text NOT NULL CHECK (length(label) BETWEEN 1 AND 200),
    status text CHECK (length(status) <= 40),
    department_id uuid,
    vis_scope text NOT NULL CHECK (vis_scope IN ('company', 'projects', 'finance', 'hr', 'it', 'document')),
    vis_department_id uuid,
    vis_user_id uuid,
    active boolean NOT NULL,
    projected_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, kind, ref_id),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, department_id) REFERENCES core.departments(company_id, id),
    FOREIGN KEY (company_id, vis_department_id) REFERENCES core.departments(company_id, id),
    CHECK (vis_scope NOT IN ('projects', 'document') OR vis_department_id IS NOT NULL)
);

CREATE TABLE core.graph_relationships (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    src_id uuid NOT NULL,
    dst_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind ~ '^[a-z_]{2,40}$'),
    authority text NOT NULL CHECK (authority IN ('record', 'document_mention', 'reviewed')),
    document_id uuid,
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    ended_reason text CHECK (ended_reason IN ('source_changed', 'source_archived', 'document_superseded')),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, src_id) REFERENCES core.graph_entities(company_id, id),
    FOREIGN KEY (company_id, dst_id) REFERENCES core.graph_entities(company_id, id),
    FOREIGN KEY (company_id, document_id) REFERENCES core.documents(company_id, id),
    CHECK (src_id <> dst_id),
    CHECK ((valid_to IS NULL) = (ended_reason IS NULL)),
    CHECK (valid_to IS NULL OR valid_to >= valid_from),
    CHECK ((authority = 'record') = (document_id IS NULL))
);
CREATE UNIQUE INDEX graph_relationships_current_uq ON core.graph_relationships (company_id, src_id, dst_id, kind)
    WHERE valid_to IS NULL;
CREATE INDEX graph_relationships_src_idx ON core.graph_relationships (company_id, src_id) WHERE valid_to IS NULL;
CREATE INDEX graph_relationships_dst_idx ON core.graph_relationships (company_id, dst_id) WHERE valid_to IS NULL;

CREATE TABLE core.graph_evidence (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    relationship_id uuid NOT NULL,
    source_type text NOT NULL CHECK (source_type IN ('project', 'task', 'purchase_request', 'leave_request',
                                                     'access_request', 'document', 'document_version')),
    source_id uuid NOT NULL,
    document_version_id uuid,
    quote text CHECK (length(quote) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, relationship_id) REFERENCES core.graph_relationships(company_id, id),
    FOREIGN KEY (company_id, document_version_id) REFERENCES core.document_versions(company_id, id),
    CHECK ((source_type = 'document_version') = (document_version_id IS NOT NULL AND document_version_id = source_id)),
    CHECK ((source_type = 'document_version') = (quote IS NOT NULL))
);
CREATE INDEX graph_evidence_relationship_idx ON core.graph_evidence (relationship_id);

-- GRAPH-03: an edge without evidence cannot be committed.
CREATE FUNCTION core.graph_relationship_has_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM core.graph_evidence e WHERE e.relationship_id = NEW.id) THEN
        RAISE EXCEPTION 'graph relationship without evidence' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER graph_relationship_evidence AFTER INSERT ON core.graph_relationships
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION core.graph_relationship_has_evidence();

-- GRAPH-05: an ended edge stays ended; history is not rewritten.
CREATE FUNCTION core.graph_relationship_end_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.valid_to IS NOT NULL THEN
        RAISE EXCEPTION 'graph relationship already ended' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER graph_relationship_end_once BEFORE UPDATE ON core.graph_relationships
    FOR EACH ROW EXECUTE FUNCTION core.graph_relationship_end_once();

CREATE TABLE core.graph_candidates (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    document_id uuid NOT NULL,
    document_version_id uuid NOT NULL,
    src_id uuid NOT NULL,
    dst_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('depends_on', 'blocked_by', 'delays', 'affects')),
    quote text NOT NULL CHECK (length(quote) BETWEEN 3 AND 500),
    extractor text NOT NULL CHECK (length(extractor) BETWEEN 1 AND 160),
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'accepted', 'rejected', 'superseded')),
    reviewed_by uuid,
    reviewed_at timestamptz,
    relationship_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_version_id, src_id, dst_id, kind),
    FOREIGN KEY (company_id, document_id) REFERENCES core.documents(company_id, id),
    FOREIGN KEY (company_id, document_version_id) REFERENCES core.document_versions(company_id, id),
    FOREIGN KEY (company_id, src_id) REFERENCES core.graph_entities(company_id, id),
    FOREIGN KEY (company_id, dst_id) REFERENCES core.graph_entities(company_id, id),
    FOREIGN KEY (company_id, reviewed_by) REFERENCES core.memberships(company_id, user_id),
    FOREIGN KEY (company_id, relationship_id) REFERENCES core.graph_relationships(company_id, id),
    CHECK (src_id <> dst_id),
    CHECK ((status IN ('accepted', 'rejected')) = (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)),
    CHECK ((status = 'accepted') = (relationship_id IS NOT NULL))
);
CREATE INDEX graph_candidates_proposed_idx ON core.graph_candidates (company_id, document_id) WHERE status = 'proposed';

CREATE TABLE core.graph_extraction_runs (
    document_version_id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('done', 'failed')),
    attempts integer NOT NULL DEFAULT 1 CHECK (attempts BETWEEN 1 AND 100),
    error_code text CHECK (error_code ~ '^[A-Z_]{1,40}$'),
    candidates integer NOT NULL DEFAULT 0 CHECK (candidates >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, document_version_id) REFERENCES core.document_versions(company_id, id),
    CHECK ((status = 'failed') = (error_code IS NOT NULL))
);

CREATE TABLE core.graph_projection_state (
    company_id uuid PRIMARY KEY REFERENCES core.companies(id),
    last_started_at timestamptz,
    last_finished_at timestamptz,
    last_status text CHECK (last_status IN ('ok', 'failed')),
    last_error_code text CHECK (last_error_code ~ '^[A-Z_]{1,40}$'),
    last_changes integer NOT NULL DEFAULT 0 CHECK (last_changes >= 0),
    runs bigint NOT NULL DEFAULT 0 CHECK (runs >= 0)
);
"""

UPDATABLE = {
    "graph_entities": "label, status, department_id, vis_scope, vis_department_id, vis_user_id, active, projected_at",
    "graph_relationships": "valid_to, ended_reason",
    "graph_candidates": "status, reviewed_by, reviewed_at, relationship_id",
    "graph_extraction_runs": "status, attempts, error_code, candidates, updated_at",
    "graph_projection_state": "last_started_at, last_finished_at, last_status, last_error_code, last_changes, runs",
}


def upgrade() -> None:
    op.execute(SCHEMA)
    for table in TABLES:
        op.execute(f"ALTER TABLE core.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE core.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON core.{table} USING (company_id = core.current_company_id())"
                   f" WITH CHECK (company_id = core.current_company_id())")
        op.execute(f"GRANT SELECT, INSERT ON core.{table} TO {RUNTIME_ROLE}")
    for table, columns in UPDATABLE.items():
        op.execute(f"GRANT UPDATE ({columns}) ON core.{table} TO {RUNTIME_ROLE}")
    # Document-relationship extraction is a model call like the assistant's; it is recorded the same way.
    op.execute("ALTER TABLE core.ai_runs DROP CONSTRAINT ai_runs_stage_check")
    op.execute("ALTER TABLE core.ai_runs ADD CONSTRAINT ai_runs_stage_check CHECK (stage IN ('plan', 'answer', 'extract'))")


def downgrade() -> None:
    op.execute("ALTER TABLE core.ai_runs DROP CONSTRAINT ai_runs_stage_check")
    op.execute("ALTER TABLE core.ai_runs ADD CONSTRAINT ai_runs_stage_check CHECK (stage IN ('plan', 'answer'))")
    for table in ("graph_projection_state", "graph_extraction_runs", "graph_candidates", "graph_evidence"):
        op.execute(f"DROP TABLE core.{table}")
    op.execute("DROP TABLE core.graph_relationships")
    op.execute("DROP FUNCTION core.graph_relationship_end_once()")
    op.execute("DROP FUNCTION core.graph_relationship_has_evidence()")
    op.execute("DROP TABLE core.graph_entities")
